import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ.setdefault("NETMAP_PROFILE", os.path.join(ROOT, "profile.example"))
sys.path.insert(0, ROOT)

import config  # noqa: E402


def write(d, text):
    p = os.path.join(d, "config.toml")
    with open(p, "w") as f:
        f.write(text)
    return p


class MergeTests(unittest.TestCase):
    def test_nested_override_keeps_siblings(self):
        out = config._merge({"a": {"x": 1, "y": 2}, "b": 3}, {"a": {"y": 9}})
        self.assertEqual(out, {"a": {"x": 1, "y": 9}, "b": 3})

    def test_does_not_mutate_base(self):
        base = {"a": {"x": 1}}
        config._merge(base, {"a": {"x": 2}})
        self.assertEqual(base, {"a": {"x": 1}})

    def test_none_override(self):
        self.assertEqual(config._merge({"a": 1}, None), {"a": 1})


class SectionTests(unittest.TestCase):
    def test_attr_and_item_access(self):
        s = config.Section({"nas": {"host": "h"}})
        self.assertEqual(s.nas.host, "h")
        self.assertEqual(s["nas"]["host"], "h")

    def test_missing_attr_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            config.Section({}).nope


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for k in ("NETMAP_NAS_PASS", "NETMAP_HA_TOKEN"):
            self.addCleanup(os.environ.pop, k, None)
            os.environ.pop(k, None)

    def test_missing_file(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(os.path.join(self.tmp.name, "nope.toml"))

    def test_short_config_gets_defaults(self):
        cfg = config.load_config(write(self.tmp.name, '[site]\nname = "Lab"\n'))
        self.assertEqual(cfg.site.name, "Lab")
        self.assertFalse(cfg.nas.enabled)
        self.assertEqual(cfg.site.layers["pc"], "PC")

    def test_layer_names_overridable(self):
        cfg = config.load_config(write(self.tmp.name, '[site.layers]\npc = "Desk"\n'))
        self.assertEqual(cfg.site.layers["pc"], "Desk")
        self.assertEqual(cfg.site.layers["nas"], "Server")

    def test_nas_enabled_requires_host(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(write(self.tmp.name, "[nas]\nenabled = true\n"))

    def test_ha_enabled_requires_url(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(write(self.tmp.name, "[homeassistant]\nenabled = true\n"))

    def test_secret_env_beats_file(self):
        sf = os.path.join(self.tmp.name, "pw")
        open(sf, "w").write("fromfile\n")
        p = write(self.tmp.name, '[nas]\npassword_file = "%s"\n' % sf)
        self.assertEqual(config.load_config(p).nas.password, "fromfile")
        os.environ["NETMAP_NAS_PASS"] = " fromenv "
        self.assertEqual(config.load_config(p).nas.password, "fromenv")

    def test_no_secret_is_empty(self):
        self.assertEqual(config.load_config(write(self.tmp.name, "")).nas.password, "")


class ExampleProfileTests(unittest.TestCase):
    def test_curated_has_all_tables(self):
        cur = config.load_curated()
        for name in ("CONTAINERS", "EDGES", "TRAPS", "NOTES", "PC_GROUPS"):
            self.assertTrue(hasattr(cur, name), name)


if __name__ == "__main__":
    unittest.main()
