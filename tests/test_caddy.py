import os
import shutil
import tempfile
import textwrap
import unittest

from redh import caddy


class UpstreamTest(unittest.TestCase):
    def test_parse_upstream(self):
        self.assertEqual(caddy.parse_upstream("127.0.0.1:8080"), ("127.0.0.1", 8080))
        self.assertEqual(caddy.parse_upstream("http://localhost:3000"), ("localhost", 3000))
        self.assertEqual(caddy.parse_upstream("[::1]:9000"), ("::1", 9000))
        self.assertEqual(caddy.parse_upstream(":8081"), ("", 8081))
        self.assertIsNone(caddy.parse_upstream("localhost"))
        self.assertIsNone(caddy.parse_upstream("unix//run/app.sock"))
        self.assertIsNone(caddy.parse_upstream("{env.UPSTREAM}"))

    def test_proxy_targets(self):
        text = textwrap.dedent("""
            app.example.org {
                @api path /api/*
                reverse_proxy @api 127.0.0.1:9000   # comment
                reverse_proxy {
                    to localhost:8080 localhost:8081
                    lb_policy first
                }
                # reverse_proxy 127.0.0.1:1
                handle /x {
                    reverse_proxy 10.0.0.5:80
                }
            }
            """)
        self.assertEqual(caddy.proxy_targets(text), [
            (4, "127.0.0.1", 9000), (6, "localhost", 8080), (6, "localhost", 8081),
            (11, "10.0.0.5", 80)])

    def test_parse_publish(self):
        self.assertEqual(caddy.parse_publish("8080:80"), ("", 8080, 8080, "80"))
        self.assertEqual(caddy.parse_publish("127.0.0.1:8080:80/tcp"), ("127.0.0.1", 8080, 8080, "80/tcp"))
        self.assertEqual(caddy.parse_publish("[::1]:8443:443"), ("::1", 8443, 8443, "443"))
        self.assertEqual(caddy.parse_publish("8000-8002:8000-8002"), ("", 8000, 8002, "8000-8002"))
        self.assertIsNone(caddy.parse_publish("80"))
        self.assertIsNone(caddy.parse_publish("127.0.0.1::80"))


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redh-caddy-test-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(text).lstrip())
        return os.path.realpath(path)

    def test_imports_and_isolation(self):
        a = self.write("apps/a/caddy/site.caddy", "a.example.org {\n}\n")
        b = self.write("apps/b/caddy/site.caddy", "b.example.org {\n}\n")
        common = self.write("etc/snippets/common.caddy", "(tls) {\n}\n")
        sites = self.write("etc/sites.caddy", f"import {self.tmp}/apps/*/caddy/site.caddy\n")
        main = self.write("etc/Caddyfile", """
            import snippets/*.caddy
            *.example.org {
                import sites.caddy
            }
            import tls
            """)
        found = caddy.imported_files(main)
        self.assertEqual(set(found), {a, b, common, sites})
        self.assertEqual(found[a].file, sites)
        self.assertEqual(found[a].pattern, f"{self.tmp}/apps/*/caddy/site.caddy")
        self.assertEqual([e.pattern.rsplit("/", 1)[-1] for e in caddy.scan_imports(main)],
                         ["*.caddy", "sites.caddy", "site.caddy"])   # 'import tls' is a snippet

        work = tempfile.mkdtemp(dir=self.tmp)
        isolator = caddy.Isolator(main, {a, b}, work)
        config = isolator.build(b)
        with open(config) as fh:
            top = fh.read()
        self.assertIn(f"import {common}", top)          # relative import made absolute
        self.assertIn("    import ", top)                # indentation kept inside the site block
        self.assertIn("import tls", top)                 # named snippets untouched
        nested = [p for p, original in isolator.originals.items() if original == sites][0]
        with open(nested) as fh:
            self.assertEqual(fh.read().split(), ["import", b])
        self.assertIsNone(caddy.Isolator(main, {a, b}, work).build(os.path.join(self.tmp, "nope")))

    def test_parse_output(self):
        errors, records = caddy.parse_output(
            '{"level":"warn","msg":"Caddyfile input is not formatted","file":"/x","line":2}\n'
            "Error: adapting config using caddyfile: /x:3: unrecognized directive: foo\n")
        self.assertEqual(errors, ["adapting config using caddyfile: /x:3: unrecognized directive: foo"])
        self.assertEqual(records[0]["line"], 2)


if __name__ == "__main__":
    unittest.main()
