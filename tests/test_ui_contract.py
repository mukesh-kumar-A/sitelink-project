from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UserExperienceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")
        cls.app = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.ai_ui = (ROOT / "sitelink-ai-ui.js").read_text(encoding="utf-8")
        cls.execution_ui = (ROOT / "execution-intelligence-ui.js").read_text(encoding="utf-8")

    def test_grouped_navigation_preserves_every_existing_view(self):
        views = set(re.findall(r'data-view="([^"]+)"', self.html))
        self.assertEqual(
            views,
            {
                "overview", "today", "inbox", "agent", "schedule", "plans", "areas",
                "risks", "conflicts", "missing", "execution", "sitelink-ai", "memory", "insights",
                "audit", "users",
            },
        )
        self.assertIn('data-nav-group="capture-review"', self.html)
        self.assertIn('data-nav-group="ai"', self.html)
        self.assertLess(self.html.index('data-nav-group="capture-review"'), self.html.index('data-nav-group="ai"'))
        self.assertIn('data-nav-group="capture-review"', self.app)
        self.assertIn('data-nav-group="attention"', self.html)
        self.assertIn('data-nav-group="governance"', self.html)
        ai_section = self.html.split('data-nav-group="ai"', 1)[1].split('</details>', 1)[0]
        self.assertNotIn('data-view="agent"', ai_section)
        self.assertIn('data-view="sitelink-ai"', ai_section)
        self.assertIn('data-view="memory"', ai_section)
        self.assertIn('data-view="insights"', ai_section)
        capture_section = self.html.split('data-nav-group="capture-review"', 1)[1].split('</details>', 1)[0]
        self.assertIn('Capture &amp; Review', capture_section)
        self.assertIn('data-nav-group="capture-review" open', self.html)
        self.assertIn('activeNavGroup.open=true', self.app)
        self.assertIn('data-view="today"', capture_section)
        self.assertIn('data-view="inbox"', capture_section)
        self.assertIn('class="nav-item nav-ai-entry" data-view="agent"', self.html)
        self.assertIn('class="nav-ai-badge" aria-hidden="true">AI</span>', self.html)
        self.assertLess(self.html.index('</details>', self.html.index('data-nav-group="capture-review"')), self.html.index('data-view="agent"'))
        self.assertLess(self.html.index('data-view="agent"'), self.html.index('data-nav-group="attention"'))

    def test_global_home_and_activity_ask_controls_use_scoped_site_link_ai(self):
        self.assertIn('id="globalAskButton"', self.html)
        self.assertIn('data-view="sitelink-ai"', self.html)
        self.assertIn("navigate('sitelink-ai')", self.app)
        self.assertIn("var route = areaId ? '/api/ai/investigate' : '/api/ai/ask'", self.ai_ui)
        self.assertIn("api.api(route, 'POST', payload)", self.ai_ui)
        self.assertIn("data-detail-ai", self.app)
        self.assertIn('data-agent-action="memory"', self.app)

    def test_activity_detail_keeps_approval_and_evidence_distinctions(self):
        self.assertIn("function openActivityDetail(id)", self.app)
        self.assertIn("Supporting extracted evidence", self.app)
        self.assertIn("Suggested match only; this report has not updated schedule actuals.", self.app)
        self.assertIn("Planner-approved reports are linked to actuals.", self.app)

    def test_cpm_graph_is_progressively_disclosed(self):
        self.assertIn('class="ei-advanced"', self.execution_ui)
        self.assertIn("CPM, dependencies, and critical path", self.execution_ui)
        self.assertIn("card('Live dependency graph', renderGraph(a))", self.execution_ui)

    def test_project_home_uses_authorized_deterministic_project_summaries(self):
        home = (ROOT / "sitelink-home-ui.js").read_text(encoding="utf-8")
        self.assertIn("/api/project-control?projectId=", home)
        self.assertIn("/api/execution-intelligence?projectId=", home)
        self.assertIn("progress.actual", home)
        self.assertIn("forecast.estimatedCompletion", home)
        self.assertIn("execution.thread", home)
        self.assertIn("Execution digital thread", home)
        self.assertIn("insufficient approved history", home.lower())

    def test_home_decisions_and_insights_require_stored_project_records(self):
        home = (ROOT / "sitelink-home-ui.js").read_text(encoding="utf-8")
        self.assertIn("item.status === 'Proposed'", home)
        self.assertIn("execution.analysis", home)
        self.assertIn("cause.sourceRecords", home)
        self.assertIn("cause.evidenceIds", home)
        self.assertIn("goButton('Open analysis', 'execution')", home)
        self.assertIn("Heuristic inference", home)

    def test_project_memory_includes_measured_recovery_outcomes_and_lessons(self):
        home = (ROOT / "sitelink-home-ui.js").read_text(encoding="utf-8")
        execution_ui = (ROOT / "execution-intelligence-ui.js").read_text(encoding="utf-8")
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("renderExistingMemory", home)
        self.assertIn("execution.outcomes", home)
        self.assertIn("p.lesson", home)
        self.assertIn(".ei-outcome-lesson", execution_ui)
        self.assertIn('"lesson": lesson', server)


if __name__ == "__main__":
    unittest.main()
