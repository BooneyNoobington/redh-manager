# redh, the Rust port: one static binary.
#
#   make                  build target/x86_64-unknown-linux-musl/release/redh
#   make test             run the tests
#   sudo make install     copy the binary to /usr/local/sbin/redh
#   sudo make uninstall
#
# Build as yourself, install as root: 'install' only copies what 'make' built,
# so cargo never runs as root.

PREFIX ?= /usr/local
SBINDIR ?= $(PREFIX)/sbin
TARGET ?= x86_64-unknown-linux-musl
CARGO ?= cargo
BIN := target/$(TARGET)/release/redh

all: build

build:
	$(CARGO) build --release --target $(TARGET)

test:
	$(CARGO) test

check:
	$(CARGO) fmt --check
	$(CARGO) clippy --all-targets -- -D warnings

install:
	@test -x $(BIN) || { echo "$(BIN) is missing: run 'make' first, as yourself"; exit 1; }
	install -D -m 0755 $(BIN) $(DESTDIR)$(SBINDIR)/redh

uninstall:
	rm -f $(DESTDIR)$(SBINDIR)/redh

clean:
	$(CARGO) clean

.PHONY: all build test check install uninstall clean
