"""来源样本和本地HTTP服务的回归检查。"""
import hashlib
import json
import unittest
from urllib.request import urlopen
from source_site.source import Source
from source_site.__main__ import running


class SourceTests(unittest.TestCase):
    def test_source_1(self):
        s = Source()
        self.assertEqual("r1", s.request("GET", "/session")[2]["revision"])

    def test_source_2(self):
        s = Source()
        r = s.request("GET", "/points", {"revision":["r1"], "bbox":["0,0,10,10"], "time":["0,5"]})[2]
        self.assertEqual(9, r["count"])
        self.assertTrue(r["truncated"])
        self.assertEqual(3, len(r["records"]))

    def test_source_3(self):
        s = Source()
        s.phase = "expired"
        self.assertEqual(410, s.request("GET", "/points", {})[0])

    def test_source_4(self):
        s = Source()
        r = s.request("GET", "/points", {"revision":["r1"], "bbox":["1,1,1,1"], "time":["2,3"]})[2]
        self.assertEqual(5, r["count"])

    def test_http_source(self):
        with running() as (base, source):
            with urlopen(base + "/session", timeout=3) as response:
                self.assertEqual(200, response.status)
                self.assertTrue(response.read())


if __name__ == "__main__":
    unittest.main()
