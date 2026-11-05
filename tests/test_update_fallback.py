"""
检查更新：GitHub API 限流（403 rate limit exceeded）时改读 version.json。
运行：python -m unittest tests.test_update_fallback
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import unittest
from unittest import mock

import requests
import updater  # noqa: E402


class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data, self.headers = status, data, {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: rate limit exceeded")

    def json(self):
        return self._data


REMOTE = {"version": "3.0.110", "commit": "63a9440" + "0" * 33, "count": 110, "updated_at": "2026-10-17 18:00:00"}


def _fake_get(url, **kwargs):
    if "api.github.com" in url:
        return _Resp(403)
    return _Resp(200, REMOTE)


class UpdateFallbackTest(unittest.TestCase):
    def test_rate_limited_api_falls_back_to_version_json(self):
        with mock.patch.object(updater.requests, "get", side_effect=_fake_get):
            remote = updater.get_remote_info({})
        self.assertEqual(remote["count"], 110)
        self.assertEqual(remote["source"], "version.json")

    def _check(self, local_count):
        local = {"version": f"3.0.{local_count}", "commit": "abc1234", "sha": "abc1234" + "0" * 33,
                 "count": local_count, "source": "git"}
        with mock.patch.object(updater.requests, "get", side_effect=_fake_get), \
                mock.patch.object(updater, "get_local_info", return_value=local):
            return updater.check_update({})

    def test_newer_remote_has_update(self):
        result = self._check(103)
        self.assertTrue(result["has_update"])
        self.assertEqual(result["remote"]["version"], updater.version_of(110))

    def test_version_numbering(self):
        # 3.1 从 BASE_COUNT 那个提交起算，三位补零；更早的提交不会算出负数
        self.assertEqual(updater.version_of(updater.BASE_COUNT), "3.1.000")
        self.assertEqual(updater.version_of(updater.BASE_COUNT + 7), "3.1.007")
        self.assertEqual(updater.version_of(updater.BASE_COUNT + 123), "3.1.123")
        self.assertEqual(updater.version_of(110), "3.1.000")

    def test_same_release_no_update(self):
        # git 用户本地多一个 bump 提交：111 对 version.json 的 110，不算有更新
        self.assertFalse(self._check(111)["has_update"])

    def test_both_fail_reports_both(self):
        with mock.patch.object(updater.requests, "get", return_value=_Resp(403)):
            with self.assertRaises(updater.UpdateError) as ctx:
                updater.get_remote_info({})
        self.assertIn("version.json", str(ctx.exception))


CHANGELOG = {"entries": [
    {"version": "3.0.108", "count": 108, "date": "2026-10-16", "title": "旧版", "items": ["a"]},
    {"version": "3.0.110", "count": 110, "date": "2026-10-17T18:00", "title": "最新", "items": ["b", " ", "c"]},
    {"version": "3.0.105", "count": 105, "title": "更旧", "items": []},
    {"count": "坏的", "title": "格式不对"},
    {"version": "3.0.109", "count": 109},
]}


class ChangelogTest(unittest.TestCase):
    """「启动器更新」卡片的更新内容：远端 changelog.json，读不到就用本地那份"""

    def _check(self, local_count, changelog_resp):
        local = {"version": f"3.0.{local_count}", "commit": "abc1234", "sha": "abc1234" + "0" * 33,
                 "count": local_count, "source": "git"}

        def fake_get(url, **kwargs):
            if "api.github.com" in url:
                return _Resp(403)
            if url.endswith("changelog.json"):
                return changelog_resp
            return _Resp(200, REMOTE)

        with mock.patch.object(updater.requests, "get", side_effect=fake_get), \
                mock.patch.object(updater, "get_local_info", return_value=local), \
                mock.patch.object(updater, "local_changelog", return_value=[{"title": "本地"}]):
            return updater.check_update({})

    def test_parse_sorts_and_drops_bad_entries(self):
        entries = updater._parse_changelog(CHANGELOG)
        self.assertEqual([e["count"] for e in entries], [110, 108, 105])
        self.assertEqual(entries[0]["items"], ["b", "c"])
        self.assertEqual(entries[0]["date"], "2026-10-17")

    def test_update_lists_newer_entries(self):
        result = self._check(107, _Resp(200, CHANGELOG))
        self.assertTrue(result["has_update"])
        self.assertEqual([c["title"] for c in result["changes"]], ["最新", "旧版"])

    def test_up_to_date_shows_current_entry(self):
        result = self._check(111, _Resp(200, CHANGELOG))
        self.assertFalse(result["has_update"])
        self.assertEqual([c["title"] for c in result["changes"]], ["最新"])

    def test_remote_changelog_missing_uses_local(self):
        result = self._check(111, _Resp(404))
        self.assertEqual(result["changes"], [{"title": "本地"}])

    def test_unknown_local_version_lists_latest(self):
        result = self._check(0, _Resp(200, CHANGELOG))
        self.assertEqual(len(result["changes"]), 3)

    def test_repo_changelog_is_valid(self):
        with open(updater.CHANGELOG_FILE, encoding="utf-8") as f:
            import json
            raw = json.load(f)["entries"]
        entries = updater._parse_changelog({"entries": raw})
        self.assertEqual(len(entries), len(raw))   # 每条都合格
        self.assertTrue(all(e["version"] and e["count"] and e["title"] for e in entries))


if __name__ == "__main__":
    unittest.main()
