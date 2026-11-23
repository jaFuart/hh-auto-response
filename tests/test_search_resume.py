"""Search URLs stay bound to the selected resume and keep permissive GPH searches broad."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from hh_auto.core.config import Config, Filters, Search
from hh_auto.hh.filters import HhFilters
from hh_auto.hh.search import build_search_url, search_urls


class SearchResumeBindingTest(unittest.TestCase):
    def params(self, cfg, text=None):
        return parse_qs(urlparse(build_search_url(cfg, text)).query, keep_blank_values=True)

    def test_resume_mode_replaces_stale_ids_and_keeps_existing_filters(self):
        cfg = Config(search_url="https://hh.ru/search/vacancy?resume=old&resume=older&text=Python&area=1&area=2"
                                "&work_format=REMOTE&custom=&hhtmFrom=resume_list",
                     resume_url="https://hh.ru/resume/selectedid")
        params = self.params(cfg)
        self.assertEqual(params["resume"], ["selectedid"])
        self.assertEqual(params["text"], ["Python"])
        self.assertEqual(params["area"], ["1", "2"])
        self.assertEqual(params["work_format"], ["REMOTE"])
        self.assertEqual(params["custom"], [""])
        self.assertEqual(params["hhtmFrom"], ["resume_list"])

    def test_query_mode_binds_resume_and_preserves_query_and_configured_filters(self):
        cfg = Config(search_url="https://hh.ru/search/vacancy?resume=old", resume_url="https://hh.ru/resume/newid",
                     search=Search(mode="query", query="Python\nBackend", area="113", title_only=True),
                     hh_filters=HhFilters(work_format=["REMOTE"], salary=150000))
        params = self.params(cfg, "Backend")
        self.assertEqual(params["resume"], ["newid"])
        self.assertEqual(params["text"], ["Backend"])
        self.assertEqual(params["area"], ["113"])
        self.assertEqual(params["search_field"], ["name"])
        self.assertEqual(params["work_format"], ["REMOTE"])
        self.assertEqual(params["salary"], ["150000"])
        for _, url in search_urls(cfg):
            self.assertEqual(parse_qs(urlparse(url).query)["resume"], ["newid"])

    def test_no_selected_url_uses_id_from_search_link(self):
        cfg = Config(search_url="https://hh.ru/search/vacancy?resume=fromlink&text=Python")
        self.assertEqual(self.params(cfg)["resume"], ["fromlink"])

    def test_no_id_preserves_unbound_searches(self):
        for mode in ("resume", "query"):
            with self.subTest(mode=mode):
                cfg = Config(search_url="https://hh.ru/search/vacancy?text=Python&accept_temporary=true",
                             search=Search(mode=mode, query="Python", area="1"))
                params = self.params(cfg)
                self.assertNotIn("resume", params)
                self.assertEqual(params["text"], ["Python"])
                if mode == "resume":
                    self.assertEqual(params["accept_temporary"], ["true"])
                else:
                    self.assertEqual(params["area"], ["1"])


class PermissiveGphSearchTest(unittest.TestCase):
    def params(self, cfg):
        return parse_qs(urlparse(build_search_url(cfg)).query)

    def test_permissive_mode_removes_link_and_configured_temporary_filter(self):
        for mode in ("resume", "query"):
            for configured in (False, True):
                with self.subTest(mode=mode, configured=configured):
                    cfg = Config(search_url="https://hh.ru/search/vacancy?text=Python&accept_temporary=true"
                                            "&accept_temporary=false&work_format=REMOTE",
                                 resume_url="https://hh.ru/resume/chosen",
                                 search=Search(mode=mode, query="Python", area="1"),
                                 filters=Filters(require_gph=True, gph_allow_unspecified=True),
                                 hh_filters=HhFilters(accept_temporary=configured, salary=100000))
                    params = self.params(cfg)
                    self.assertNotIn("accept_temporary", params)
                    self.assertEqual(params["resume"], ["chosen"])
                    self.assertEqual(params["text"], ["Python"])
                    self.assertEqual(params["salary"], ["100000"])
                    self.assertEqual(cfg.hh_filters.accept_temporary, configured)

    def test_temporary_filter_is_kept_unless_both_flags_are_enabled(self):
        for require_gph, allow_unspecified in ((False, False), (True, False), (False, True)):
            for mode in ("resume", "query"):
                with self.subTest(require_gph=require_gph, allow_unspecified=allow_unspecified, mode=mode):
                    cfg = Config(search_url="https://hh.ru/search/vacancy?accept_temporary=true",
                                 search=Search(mode=mode, query="Python"),
                                 filters=Filters(require_gph=require_gph, gph_allow_unspecified=allow_unspecified),
                                 hh_filters=HhFilters(accept_temporary=True))
                    self.assertEqual(self.params(cfg)["accept_temporary"], ["true"])

    def test_permissive_mode_does_not_add_filter_or_resume(self):
        cfg = Config(search_url="https://hh.ru/search/vacancy?text=Python&area=2",
                     filters=Filters(require_gph=True, gph_allow_unspecified=True))
        params = self.params(cfg)
        self.assertNotIn("accept_temporary", params)
        self.assertNotIn("resume", params)
        self.assertEqual(params["area"], ["2"])


if __name__ == "__main__":
    unittest.main()
