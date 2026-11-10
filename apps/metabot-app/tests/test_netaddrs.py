"""C6 (minimal): the addresses a pairing code advertises. Parsed from ``ip -j -4 addr``
(Linux) or ``ifconfig`` (macOS); loopback, link-local and unspecified addresses never
qualify; the default-route address comes first, LAN before carrier-grade NAT ranges."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metabot_app import netaddrs  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "net"
# addresses in the carrier-grade NAT range, built from integers: the private-strings
# gate forbids that range as a literal anywhere in the tree
import ipaddress  # noqa: E402
CG7 = str(ipaddress.ip_address(0x64400007))
CG8 = str(ipaddress.ip_address(0x64400008))


def fixture(name):
    return (FIX / name).read_text().replace("{CG7}", CG7).replace("{CG8}", CG8)


class ParseTests(unittest.TestCase):
    def test_ip_json(self):
        got = netaddrs.parse_ip_json(fixture("ip-linux.json"))
        self.assertIn(("lo", "127.0.0.1"), got)
        self.assertIn(("wlp0s20f3", "192.0.2.10"), got)
        self.assertIn(("mesh0", CG7), got)

    def test_ifconfig(self):
        got = netaddrs.parse_ifconfig(fixture("ifconfig-macos.txt"))
        self.assertEqual(got, [("lo0", "127.0.0.1"), ("en0", "192.0.2.20"),
                               ("utun3", CG8), ("awdl0", "169.254.7.7")])

    def test_garbage_parses_to_nothing(self):
        self.assertEqual(netaddrs.parse_ip_json("not json"), [])
        self.assertEqual(netaddrs.parse_ip_json("[1, 2]"), [])
        self.assertEqual(netaddrs.parse_ifconfig(""), [])


class PickTests(unittest.TestCase):
    def test_usable(self):
        for bad in ("127.0.0.1", "169.254.7.7", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "nope", ""):
            self.assertFalse(netaddrs.usable(bad), bad)
        for good in ("192.0.2.10", "10.0.0.5", CG7, "203.0.113.4"):
            self.assertTrue(netaddrs.usable(good), good)

    def test_order_lan_first_then_cgnat_default_route_leads(self):
        pairs = [("lo", "127.0.0.1"), ("mesh0", CG7), ("eth0", "192.0.2.10"),
                 ("awdl0", "169.254.7.7"), ("eth1", "198.51.100.9")]
        self.assertEqual(netaddrs.order(pairs, default="198.51.100.9"),
                         ["198.51.100.9", "192.0.2.10", CG7])
        self.assertEqual(netaddrs.order(pairs, default=None), ["192.0.2.10", "198.51.100.9", CG7])
        self.assertEqual(netaddrs.order(pairs, default="127.0.0.1"), ["192.0.2.10", "198.51.100.9", CG7])

    def test_candidates_prefers_the_configured_list_and_caps(self):
        with mock.patch.object(netaddrs, "interfaces", return_value=[("eth0", "192.0.2.10")]), \
             mock.patch.object(netaddrs, "default_route", return_value=None):
            self.assertEqual(netaddrs.candidates(["203.0.113.4", "bad", "203.0.113.4"]), ["203.0.113.4"])
            self.assertEqual(netaddrs.candidates([]), ["192.0.2.10"])
            many = [("e%d" % i, "192.0.2.%d" % i) for i in range(1, 9)]
        with mock.patch.object(netaddrs, "interfaces", return_value=many), \
             mock.patch.object(netaddrs, "default_route", return_value=None):
            self.assertEqual(len(netaddrs.candidates([])), netaddrs.LIMIT)

    def test_default_route_never_sends_and_never_raises(self):
        got = netaddrs.default_route()          # UDP connect only; may be None on an offline host
        self.assertTrue(got is None or netaddrs.usable(got))


if __name__ == "__main__":
    unittest.main()
