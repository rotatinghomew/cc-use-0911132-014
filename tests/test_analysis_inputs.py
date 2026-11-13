import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_revised_plan(self):
        data = self.load_data()
        self.assertTrue(any('supersedes' in row for row in data['plans']))

    def test_revised_response(self):
        data = self.load_data()
        self.assertTrue(any('supersedes' in row for row in data['responses']))

    def test_team_transfer(self):
        data = self.load_data()
        self.assertTrue(data['transfers'][0]['from_team'] != data['transfers'][0]['to_team'])

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
