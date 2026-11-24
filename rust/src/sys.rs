//! The few things the standard library does not offer, from libc: users and
//! groups, taking on another user's identity, access checks as that user, flock.
//! Every `unsafe` of the program lives here.

use std::ffi::{CStr, CString};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

#[derive(Clone, Debug)]
pub struct Passwd {
    pub name: String,
    pub uid: u32,
    pub gid: u32,
    pub dir: String,
}

fn cstring(text: &str) -> Option<CString> {
    CString::new(text).ok()
}

unsafe fn text(ptr: *const libc::c_char) -> String {
    if ptr.is_null() {
        return String::new();
    }
    unsafe { CStr::from_ptr(ptr) }.to_string_lossy().into_owned()
}

unsafe fn passwd_from(entry: &libc::passwd) -> Passwd {
    unsafe { Passwd { name: text(entry.pw_name), uid: entry.pw_uid, gid: entry.pw_gid, dir: text(entry.pw_dir) } }
}

fn lookup_user(
    by: impl Fn(*mut libc::passwd, *mut libc::c_char, usize, *mut *mut libc::passwd) -> libc::c_int,
) -> Option<Passwd> {
    let mut entry: libc::passwd = unsafe { std::mem::zeroed() };
    let mut buf = vec![0 as libc::c_char; 1 << 16];
    let mut result: *mut libc::passwd = std::ptr::null_mut();
    let rc = by(&mut entry, buf.as_mut_ptr(), buf.len(), &mut result);
    if rc != 0 || result.is_null() {
        return None;
    }
    Some(unsafe { passwd_from(&entry) })
}

pub fn user_by_name(name: &str) -> Option<Passwd> {
    let name = cstring(name)?;
    lookup_user(|e, b, n, r| unsafe { libc::getpwnam_r(name.as_ptr(), e, b, n, r) })
}

pub fn user_by_uid(uid: u32) -> Option<Passwd> {
    lookup_user(|e, b, n, r| unsafe { libc::getpwuid_r(uid, e, b, n, r) })
}

pub fn user_name(uid: u32) -> String {
    user_by_uid(uid).map(|p| p.name).unwrap_or_else(|| uid.to_string())
}

/// (name, listed members) of a group.
fn lookup_group(
    by: impl Fn(*mut libc::group, *mut libc::c_char, usize, *mut *mut libc::group) -> libc::c_int,
) -> Option<(String, u32, Vec<String>)> {
    let mut entry: libc::group = unsafe { std::mem::zeroed() };
    let mut buf = vec![0 as libc::c_char; 1 << 16];
    let mut result: *mut libc::group = std::ptr::null_mut();
    let rc = by(&mut entry, buf.as_mut_ptr(), buf.len(), &mut result);
    if rc != 0 || result.is_null() {
        return None;
    }
    let mut members = Vec::new();
    unsafe {
        let mut member = entry.gr_mem;
        while !member.is_null() && !(*member).is_null() {
            members.push(text(*member));
            member = member.add(1);
        }
        Some((text(entry.gr_name), entry.gr_gid, members))
    }
}

pub fn group_by_gid(gid: u32) -> Option<(String, Vec<String>)> {
    lookup_group(|e, b, n, r| unsafe { libc::getgrgid_r(gid, e, b, n, r) }).map(|(n, _, m)| (n, m))
}

pub fn gid_of_group(name: &str) -> Option<u32> {
    let name = cstring(name)?;
    lookup_group(|e, b, n, r| unsafe { libc::getgrnam_r(name.as_ptr(), e, b, n, r) }).map(|(_, g, _)| g)
}

pub fn group_name(gid: u32) -> String {
    group_by_gid(gid).map(|(n, _)| n).unwrap_or_else(|| gid.to_string())
}

/// Every user in the passwd database.
pub fn all_users() -> Vec<Passwd> {
    let mut users = Vec::new();
    unsafe {
        libc::setpwent();
        loop {
            let entry = libc::getpwent();
            if entry.is_null() {
                break;
            }
            users.push(passwd_from(&*entry));
        }
        libc::endpwent();
    }
    users
}

pub fn group_list(user: &str, gid: u32) -> Vec<u32> {
    let Some(name) = cstring(user) else { return vec![gid] };
    let mut size: libc::c_int = 64;
    loop {
        let mut groups = vec![0 as libc::gid_t; size as usize];
        let mut count = size;
        let rc = unsafe { libc::getgrouplist(name.as_ptr(), gid, groups.as_mut_ptr(), &mut count) };
        if rc >= 0 {
            groups.truncate(count as usize);
            return groups;
        }
        size = count.max(size * 2);
    }
}

pub fn geteuid() -> u32 {
    unsafe { libc::geteuid() }
}

/// Run `body` with the effective user and groups switched to another user's, as far as
/// file access goes. Only root can; returns None then (or if the switch fails).
pub fn as_identity<T>(uid: u32, gid: u32, groups: &[u32], body: impl FnOnce() -> T) -> Option<T> {
    unsafe {
        let saved_egid = libc::getegid();
        let count = libc::getgroups(0, std::ptr::null_mut());
        let mut saved = vec![0 as libc::gid_t; count.max(0) as usize];
        if count > 0 {
            libc::getgroups(count, saved.as_mut_ptr());
        }
        if libc::setgroups(groups.len() as _, groups.as_ptr()) != 0 {
            return None;
        }
        if libc::setegid(gid) != 0 || libc::seteuid(uid) != 0 {
            libc::seteuid(0);
            libc::setegid(saved_egid);
            libc::setgroups(saved.len() as _, saved.as_ptr());
            return None;
        }
        let result = body();
        libc::seteuid(0);
        libc::setegid(saved_egid);
        libc::setgroups(saved.len() as _, saved.as_ptr());
        Some(result)
    }
}

/// faccessat with AT_EACCESS: may the *effective* user do this?
pub fn effective_access(path: &Path, mode: libc::c_int) -> bool {
    let Ok(path) = CString::new(path.as_os_str().as_bytes()) else { return false };
    unsafe { libc::faccessat(libc::AT_FDCWD, path.as_ptr(), mode, libc::AT_EACCESS) == 0 }
}

/// Take an exclusive, non-blocking lock on a directory; Err(()) if someone holds it.
/// The lock lasts as long as the returned file stays open.
pub fn lock(path: &Path) -> Result<Option<std::fs::File>, ()> {
    use std::os::unix::io::AsRawFd;
    let Ok(file) = std::fs::File::open(path) else { return Ok(None) };
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(());
    }
    Ok(Some(file))
}
