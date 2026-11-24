import os
import shutil
import tempfile
import textwrap
import unittest

from redh.units import IniFile, QuadletUnit, dropin_dirs


def unit(filename: str, text: str = "", dropins=None) -> QuadletUnit:
    tmp = tempfile.mkdtemp(prefix="redh-units-")
    try:
        path = os.path.join(tmp, filename)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(text))
        for rel, body in (dropins or {}).items():
            full = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(textwrap.dedent(body))
        return QuadletUnit.load(path, [tmp])
    finally:
        shutil.rmtree(tmp)


class IniFileTest(unittest.TestCase):
    def test_sections_repeats_comments_continuations(self):
        ini = IniFile.parse(textwrap.dedent("""
            # leading comment
            [Container]
            Image=docker.io/library/nginx:1
            PublishPort=127.0.0.1:8080:80
            PublishPort=127.0.0.1:8443:443
            Exec=one \\
              # dropped inside a continuation
              two \\
              three
            ; another comment
            Label="quoted value"
            """))
        self.assertEqual(ini.get("Container", "Image"), "docker.io/library/nginx:1")
        self.assertEqual(ini.get_all("Container", "PublishPort"),
                         ["127.0.0.1:8080:80", "127.0.0.1:8443:443"])
        self.assertEqual(ini.get("Container", "Exec"), "one two three")
        self.assertEqual(ini.get("Container", "Label"), "quoted value")
        self.assertIsNone(ini.get("Container", "Missing"))

    def test_empty_assignment_resets_a_list(self):
        ini = IniFile.parse("[Install]\nWantedBy=a.target\nWantedBy=\nWantedBy=b.target c.target\n")
        self.assertEqual(ini.get_words("Install", "WantedBy"), ["b.target", "c.target"])


class NamingTest(unittest.TestCase):
    def test_service_names_follow_quadlet(self):
        self.assertEqual(unit("web.container", "[Container]\n").service, "web.service")
        self.assertEqual(unit("web.network", "[Network]\n").service, "web-network.service")
        self.assertEqual(unit("web.volume", "[Volume]\n").service, "web-volume.service")
        self.assertEqual(unit("web.pod", "[Pod]\n").service, "web-pod.service")
        self.assertEqual(unit("web.kube", "[Kube]\n").service, "web.service")
        self.assertEqual(unit("web.build", "[Build]\n").service, "web-build.service")

    def test_service_name_override(self):
        u = unit("web.container", "[Container]\nServiceName=frontend\n")
        self.assertEqual(u.service, "frontend.service")
        self.assertEqual(u.resource_name, "systemd-frontend")

    def test_templates(self):
        template = unit("worker@.container", "[Container]\n")
        self.assertEqual(template.service, "worker@.service")
        self.assertIsNone(template.startable)
        with_default = unit("worker@.container", "[Container]\n[Install]\nDefaultInstance=one\n")
        self.assertEqual(with_default.startable, "worker@one.service")
        self.assertEqual(unit("db@.volume", "[Volume]\n").service, "db-volume@.service")
        self.assertEqual(unit("worker@two.container", "[Container]\n").startable, "worker@two.service")

    def test_resource_names(self):
        self.assertEqual(unit("web.container", "[Container]\n").resource_name, "systemd-web")
        self.assertEqual(unit("web.container", "[Container]\nContainerName=web\n").resource_name, "web")
        self.assertEqual(unit("web.network", "[Network]\n").resource_name, "systemd-web")
        self.assertEqual(unit("web.volume", "[Volume]\nVolumeName=web-data\n").resource_name, "web-data")
        self.assertIsNone(unit("web.kube", "[Kube]\n").resource_name)


class ValuesTest(unittest.TestCase):
    def test_references_and_requires(self):
        u = unit("app.container", """
            [Unit]
            Requires=other.service
            [Container]
            Image=app.build
            Network=app.network:ip=10.0.0.2
            Network=host-shared.network
            Volume=app-data.volume:/data:Z
            Volume=/srv/x:/x
            Mount=type=volume,source=cache.volume,destination=/cache
            Pod=app.pod
            """)
        self.assertEqual(u.references(), [
            ("Network", "app.network"), ("Network", "host-shared.network"),
            ("Volume", "app-data.volume"), ("Mount", "cache.volume"),
            ("Pod", "app.pod"), ("Image", "app.build")])
        self.assertEqual(u.requires(), ["other.service"])

    def test_bind_sources(self):
        u = unit("app.container", """
            [Container]
            Volume=./data:/data
            Volume=/srv/app:/srv
            Volume=named:/named
            Mount=type=bind,source=/etc/app,destination=/etc/app
            """)
        self.assertEqual([s for _, s in u.bind_sources()], ["./data", "/srv/app", "/etc/app"])

    def test_dropins_merge_in_order(self):
        u = unit("web-app.container", "[Container]\nImage=a\nPublishPort=1:1\n", {
            "container.d/10-image.conf": "[Container]\nImage=b\n",
            "web-.container.d/10-image.conf": "[Container]\nImage=c\n",
            "web-app.container.d/20-port.conf": "[Container]\nPublishPort=2:2\n",
        })
        self.assertEqual(u.get("Image"), "c")      # the more specific 10-image.conf wins
        self.assertEqual(u.publish_ports(), ["1:1", "2:2"])
        self.assertEqual(len(u.dropins), 2)

    def test_dropin_dir_names(self):
        self.assertEqual(dropin_dirs("a-b-c.container"),
                         ["container.d", "a-.container.d", "a-b-.container.d", "a-b-c.container.d"])
        self.assertEqual(dropin_dirs("w@x.container"),
                         ["container.d", "w@.container.d", "w@x.container.d"])


if __name__ == "__main__":
    unittest.main()
