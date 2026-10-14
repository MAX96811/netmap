"""Tests for server.py: path safety, overrides PUT/GET, backup rotation.
Run: python3 -m unittest discover -s tests
"""
import json, os, sys, tempfile, threading, unittest, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

os.environ["QUIET"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        server.DATA = d
        server.OVERRIDES = os.path.join(d, "overrides.json")
        server.BACKUPS = os.path.join(d, "backups")
        server.LIVE = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None):
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                   data=body, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def put(self, obj):
        return self.req("PUT", "/api/overrides", json.dumps(obj).encode())

    def test_safe_path_blocks_escape(self):
        h = server.Handler.__new__(server.Handler)
        base = "/srv/web"
        self.assertEqual(h.safe_path(base, "a/b.js"), "/srv/web/a/b.js")
        self.assertIsNone(h.safe_path(base, "../secret"))
        self.assertIsNone(h.safe_path(base, "a/../../secret"))

    def test_traversal_over_http_is_not_served(self):
        code, _ = self.req("GET", "/data/../server.py")
        self.assertIn(code, (404,))

    def test_overrides_default_empty(self):
        code, body = self.req("GET", "/api/overrides")
        self.assertEqual((code, json.loads(body)), (200, {}))

    def test_put_roundtrip(self):
        self.assertEqual(self.put({"a": 1})[0], 200)
        code, body = self.req("GET", "/api/overrides")
        self.assertEqual(json.loads(body), {"a": 1})

    def test_put_rejects_non_object(self):
        self.assertEqual(self.req("PUT", "/api/overrides", b"[1,2]")[0], 400)
        self.assertEqual(self.req("PUT", "/api/overrides", b"nope")[0], 400)

    def test_backup_only_when_content_changes(self):
        self.put({"a": 1, "view": {"x": 0}})
        self.put({"a": 1, "view": {"x": 99}})   # camera move only
        self.assertFalse(os.path.isdir(server.BACKUPS) and os.listdir(server.BACKUPS))
        self.put({"a": 2, "view": {"x": 99}})   # real edit
        self.assertEqual(len(os.listdir(server.BACKUPS)), 1)

    def test_health(self):
        code, body = self.req("GET", "/api/health")
        self.assertTrue(json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main()
