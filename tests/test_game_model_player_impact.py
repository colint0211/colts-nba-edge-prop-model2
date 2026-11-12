import unittest
from unittest.mock import patch

from src.api import game_model


class PlayerImpactTests(unittest.TestCase):
    def test_official_surname_first_name_matches_bref_name(self):
        self.assertEqual(
            game_model._player_name_key("Barnes, Scottie"),
            game_model._player_name_key("Scottie Barnes"),
        )

    def test_jr_suffix_name_matches(self):
        self.assertEqual(
            game_model._player_name_key("McCullar Jr., Kevin"),
            game_model._player_name_key("Kevin McCullar Jr."),
        )

    def test_diacritic_player_names_match_ascii_injury_reports(self):
        self.assertEqual(
            game_model._player_name_key("Porziņģis, Kristaps"),
            game_model._player_name_key("Kristaps Porzingis"),
        )
        self.assertEqual(
            game_model._player_name_key("Krejčí, Vít"),
            game_model._player_name_key("Vit Krejci"),
        )
        self.assertEqual(
            game_model._player_name_key("Nurkić, Jusuf"),
            game_model._player_name_key("Jusuf Nurkic"),
        )

    def test_official_suffix_can_match_bref_without_suffix(self):
        self.assertEqual(
            game_model._player_name_key("Butler III, Jimmy"),
            game_model._player_name_key("Jimmy Butler"),
        )

    def test_confirmed_out_uses_fresh_player_impact_proxy(self):
        snapshot = {
            "available": True,
            "players": {
                "scottiebarnes|TOR": {
                    "minutes_per_game": 35.0,
                    "bpm_shrunk": 2.0,
                    "games_current_season": 10,
                }
            },
            "meta": {"replacement_bpm": -2.0, "status": "fresh"},
        }
        injury = [{
            "team": "TOR",
            "player": "Barnes, Scottie",
            "status": "Out",
            "game_date": "11/11/2026",
            "reason": "Injury/Illness",
        }]
        with patch.object(game_model, "_load_player_impact_snapshot", return_value=snapshot):
            result = game_model._injury_adjustment(
                "TOR", injury, "2026-27", "2026-11-11T19:00:00-05:00", 100.0
            )
        self.assertFalse(result["unpriced_injury"])
        self.assertTrue(result["players"][0]["impact_data_available"])
        self.assertAlmostEqual(result["points"], -(4.0 * 35.0 / 48.0), places=3)

    def test_questionable_player_remains_blocked(self):
        snapshot = {
            "available": True,
            "players": {
                "scottiebarnes|TOR": {
                    "minutes_per_game": 35.0,
                    "bpm_shrunk": 2.0,
                    "games_current_season": 10,
                }
            },
            "meta": {"replacement_bpm": -2.0, "status": "fresh"},
        }
        injury = [{
            "team": "TOR",
            "player": "Barnes, Scottie",
            "status": "Questionable",
            "game_date": "11/11/2026",
            "reason": "Injury/Illness",
        }]
        with patch.object(game_model, "_load_player_impact_snapshot", return_value=snapshot):
            result = game_model._injury_adjustment(
                "TOR", injury, "2026-27", "2026-11-11T19:00:00-05:00", 100.0
            )
        self.assertTrue(result["unpriced_injury"])
        self.assertFalse(result["players"][0]["impact_data_available"])


    def test_three_game_low_minute_player_can_be_priced_conservatively(self):
        snapshot = {
            "available": True,
            "players": {
                "tobeawaka|CHI": {
                    "minutes_per_game": 10.3,
                    "bpm_shrunk": -0.84,
                    "games_current_season": 4,
                    "bpm_prior": None,
                }
            },
            "meta": {"replacement_bpm": -2.0, "status": "fresh"},
        }
        injury = [{
            "team": "CHI",
            "player": "Awaka, Tobe",
            "status": "Out",
            "game_date": "11/11/2026",
            "reason": "Injury/Illness",
        }]
        with patch.object(game_model, "_load_player_impact_snapshot", return_value=snapshot):
            result = game_model._injury_adjustment(
                "CHI", injury, "2026-27", "2026-11-11T19:00:00-05:00", 100.0
            )
        self.assertFalse(result["unpriced_injury"])
        self.assertTrue(result["players"][0]["impact_data_available"])
        self.assertLessEqual(abs(result["points"]), 0.5)

    def test_four_game_high_minute_player_without_prior_remains_blocked(self):
        snapshot = {
            "available": True,
            "players": {
                "jamorant|POR": {
                    "minutes_per_game": 25.8,
                    "bpm_shrunk": 2.76,
                    "games_current_season": 4,
                    "bpm_prior": None,
                }
            },
            "meta": {"replacement_bpm": -2.0, "status": "fresh"},
        }
        injury = [{
            "team": "POR",
            "player": "Morant, Ja",
            "status": "Out",
            "game_date": "11/11/2026",
            "reason": "Injury/Illness",
        }]
        with patch.object(game_model, "_load_player_impact_snapshot", return_value=snapshot):
            result = game_model._injury_adjustment(
                "POR", injury, "2026-27", "2026-11-11T19:00:00-05:00", 100.0
            )
        self.assertTrue(result["unpriced_injury"])
        self.assertFalse(result["players"][0]["impact_data_available"])


    def test_unique_cross_team_player_record_is_used(self):
        snapshot = {
            "available": True,
            "players": {
                "tobiasharris|DET": {
                    "minutes_per_game": 27.7,
                    "bpm_shrunk": 1.4,
                    "bpm_prior": 1.4,
                    "games_current_season": 0,
                }
            },
            "meta": {"replacement_bpm": -2.0, "status": "fresh"},
        }
        injury = [{
            "team": "SAS",
            "player": "Harris, Tobias",
            "status": "Out",
            "game_date": "11/11/2026",
            "reason": "Injury/Illness",
        }]
        with patch.object(game_model, "_load_player_impact_snapshot", return_value=snapshot):
            result = game_model._injury_adjustment(
                "SAS", injury, "2026-27", "2026-11-11T19:00:00-05:00", 100.0
            )
        self.assertFalse(result["unpriced_injury"])
        self.assertTrue(result["players"][0]["impact_data_available"])

    def test_ambiguous_cross_team_player_records_remain_blocked(self):
        snapshot = {
            "available": True,
            "players": {
                "tobiasharris|DET": {
                    "minutes_per_game": 27.7,
                    "bpm_shrunk": 1.4,
                    "bpm_prior": 1.4,
                    "games_current_season": 0,
                },
                "tobiasharris|PHI": {
                    "minutes_per_game": 25.0,
                    "bpm_shrunk": 0.8,
                    "bpm_prior": 0.8,
                    "games_current_season": 0,
                },
            },
            "meta": {"replacement_bpm": -2.0, "status": "fresh"},
        }
        injury = [{
            "team": "SAS",
            "player": "Harris, Tobias",
            "status": "Out",
            "game_date": "11/11/2026",
            "reason": "Injury/Illness",
        }]
        with patch.object(game_model, "_load_player_impact_snapshot", return_value=snapshot):
            result = game_model._injury_adjustment(
                "SAS", injury, "2026-27", "2026-11-11T19:00:00-05:00", 100.0
            )
        self.assertTrue(result["unpriced_injury"])
        self.assertFalse(result["players"][0]["impact_data_available"])


    def test_traded_player_total_record_is_preferred(self):
        snapshot = {
            "available": True,
            "players": {
                "tobiasharris|TOT": {
                    "minutes_per_game": 26.0,
                    "bpm_shrunk": 1.1,
                    "bpm_prior": 1.1,
                    "games_current_season": 0,
                },
                "tobiasharris|DET": {
                    "minutes_per_game": 27.7,
                    "bpm_shrunk": 1.4,
                    "bpm_prior": 1.4,
                    "games_current_season": 0,
                },
            },
            "meta": {"replacement_bpm": -2.0, "status": "fresh"},
        }
        injury = [{
            "team": "SAS",
            "player": "Harris, Tobias",
            "status": "Out",
            "game_date": "11/11/2026",
            "reason": "Injury/Illness",
        }]
        with patch.object(game_model, "_load_player_impact_snapshot", return_value=snapshot):
            result = game_model._injury_adjustment(
                "SAS", injury, "2026-27", "2026-11-11T19:00:00-05:00", 100.0
            )
        self.assertFalse(result["unpriced_injury"])
        self.assertAlmostEqual(result["players"][0]["minutes"], 26.0, places=2)


if __name__ == "__main__":
    unittest.main()
