import unittest
from unittest import mock

from vpn_on_linux import vpnctl


class VpnctlUnitTests(unittest.TestCase):
    def test_yaml_quote_escapes_single_quotes(self):
        self.assertEqual(vpnctl.yaml_quote("a'b"), "'a''b'")

    def test_redact_url_hides_path_and_query(self):
        redacted = vpnctl.redact_url("https://example.com:1234/uuid/secret-token?clash=1")
        self.assertEqual(redacted, "https://example.com:1234/...?...")
        self.assertNotIn("secret-token", redacted)

    def test_targeted_config_keeps_default_match_direct(self):
        state = vpnctl.default_state()
        state["subscription_url"] = "https://example.com/sub"
        text = vpnctl.render_config_text(state)
        self.assertIn("DOMAIN-SUFFIX,openai.com,VPN", text)
        self.assertIn("DOMAIN-SUFFIX,anthropic.com,VPN", text)
        self.assertIn("MATCH,DIRECT", text)
        self.assertNotIn("MATCH,VPN", text)
        self.assertIn("allow-lan: false", text)
        self.assertIn("bind-address: 127.0.0.1", text)

    def test_global_config_routes_proxy_clients_through_vpn(self):
        state = vpnctl.default_state()
        state["subscription_url"] = "https://example.com/sub"
        state["route_mode"] = "global"
        text = vpnctl.render_config_text(state)
        self.assertIn("IP-CIDR,10.0.0.0/8,DIRECT,no-resolve", text)
        self.assertIn("MATCH,VPN", text)
        self.assertNotIn("MATCH,DIRECT", text)

    def test_proxy_env_is_localhost_only(self):
        state = vpnctl.default_state()
        env = vpnctl.proxy_env(state)
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:7890")
        self.assertIn("127.0.0.1", env["no_proxy"])
        self.assertIn("10.0.0.0/8", env["no_proxy"])

    def test_resolve_node_name_supports_index_exact_and_fuzzy(self):
        state = vpnctl.default_state()
        with mock.patch.object(vpnctl, "get_provider_nodes", return_value=["Hong Kong 01", "Tokyo 02"]):
            self.assertEqual(vpnctl.resolve_node_name("1", state), "Hong Kong 01")
            self.assertEqual(vpnctl.resolve_node_name("Tokyo 02", state), "Tokyo 02")
            self.assertEqual(vpnctl.resolve_node_name("kong", state), "Hong Kong 01")

    def test_nodes_defaults_to_list(self):
        args = type("Args", (), {"nodes_command": None})()
        state = vpnctl.default_state()
        with (
            mock.patch.object(vpnctl, "read_state", return_value=state),
            mock.patch.object(vpnctl, "get_provider_nodes", return_value=["Hong Kong 01"]),
            mock.patch.object(vpnctl, "selected_node", return_value="Hong Kong 01"),
            mock.patch("builtins.print") as print_mock,
        ):
            vpnctl.cmd_nodes(args)
        print_mock.assert_any_call("* 001 Hong Kong 01")


if __name__ == "__main__":
    unittest.main()
