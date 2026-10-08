import copy
import math
import unittest

import numpy as np

from autocst.research_optimizer import propose_bayesian


def spec(bounds=None):
    return {"objective": {"frequency_ghz": 10, "target_phase_deg": -90, "tolerance_deg": 1},
            "constraints": {}, "parameter_bounds": bounds or {"x": [0, 1]}}


def run(run_id, parameters, error, *, usable=True, feasible=True, target=False):
    return {"run_id": run_id, "state": "completed", "job": {"parameters": parameters},
            "analysis": {"usable_for_optimization": usable, "constraints_met": feasible,
                         "target_met": target, "metrics": {"phase_error_deg": error},
                         "scientific_status": {"numerical_validity": "pending", "mesh_convergence": "unknown"}}}


class BayesianOptimizerTests(unittest.TestCase):
    def test_reproducible_initial_design_and_feedback(self):
        contract = spec()
        config = {"seed": 17, "initial_samples": 3, "candidate_count": 512}
        self.assertEqual(propose_bayesian(contract, [], config), propose_bayesian(contract, [], config))
        runs = [run(str(i), {"x": x}, 100 * (x - .3) ** 2) for i, x in enumerate([0, .5, 1])]
        before = copy.deepcopy((contract, runs, config))
        first = propose_bayesian(contract, runs, config)
        self.assertEqual(first, propose_bayesian(contract, runs, config))
        self.assertEqual((contract, runs, config), before)
        self.assertEqual(first["stage"], "bayesian_optimization")
        self.assertTrue(math.isfinite(first["predicted_score"]))
        self.assertGreaterEqual(first["uncertainty"], 0)
        self.assertGreaterEqual(first["acquisition"], 0)

    def test_known_quadratic_feedback_selects_an_actual_improvement(self):
        runs = [run(str(i), {"x": x}, 100 * (x - .3) ** 2) for i, x in enumerate([0, .5, 1])]
        proposal = propose_bayesian(spec(), runs, {"seed": 7, "initial_samples": 3})
        x = proposal["parameters"]["x"]
        self.assertLess(100 * (x - .3) ** 2, min(item["analysis"]["metrics"]["phase_error_deg"] for item in runs))
        self.assertEqual(proposal["training_run_ids"], ["0", "1", "2"])

    def test_initial_design_is_bounded_and_does_not_repeat(self):
        contract = spec({"a": {"min": -2, "max": 4}, "b": [100, 150]})
        runs = []
        for index in range(5):
            proposal = propose_bayesian(contract, runs, {"seed": 3, "initial_samples": 5})
            point = proposal["parameters"]
            self.assertTrue(-2 <= point["a"] <= 4 and 100 <= point["b"] <= 150)
            self.assertNotIn(point, [item["job"]["parameters"] for item in runs])
            runs.append(run(str(index), point, 20 + index))

    def test_numerically_invalid_and_nonfinite_scores_are_excluded(self):
        invalid = run("invalid", {"x": .4}, 0, usable=False, target=True)
        valid = run("valid", {"x": .2}, 8)
        nonfinite = run("nan", {"x": .7}, float("nan"))
        proposal = propose_bayesian(spec(), [invalid, valid, nonfinite], {})
        self.assertEqual(proposal["action"], "submit")
        self.assertEqual(proposal["training_run_ids"], ["valid"])
        self.assertEqual({item["run_id"] for item in proposal["excluded_runs"]}, {"invalid", "nan"})
        self.assertNotEqual(proposal["parameters"]["x"], .4)
        self.assertNotEqual(proposal["parameters"]["x"], .7)

    def test_explicit_backend_invalidity_overrides_a_stale_usable_flag(self):
        bad = run("bad", {"x": .5}, 0, target=True)
        bad["details"] = {"result": {"numerical_validity": {"passed": False}}}
        proposal = propose_bayesian(spec(), [bad], {})
        self.assertEqual(proposal["action"], "submit")
        self.assertEqual(proposal["training_run_ids"], [])
        self.assertEqual(proposal["excluded_runs"][0]["reason"], "backend_numerical_check_failed")

    def test_constraints_use_auditable_finite_penalty_and_never_false_stop(self):
        bad = run("infeasible", {"x": .2}, 0, feasible=False, target=True)
        bad["analysis"]["constraints"] = {"min_reflection_magnitude": {"passed": False, "value": .2, "threshold": .9}}
        good = run("feasible", {"x": .8}, 179)
        proposal = propose_bayesian(spec(), [bad, good], {"initial_samples": 2})
        observation = proposal["training_observations"][0]
        self.assertEqual(proposal["action"], "submit")
        self.assertEqual(proposal["penalized_run_ids"], ["infeasible"])
        self.assertGreater(observation["score"], 180)
        self.assertTrue(math.isfinite(observation["score"]))
        self.assertEqual(observation["phase_error_deg"], 0)
        self.assertFalse(observation["constraints_met"])

    def test_failed_constraint_details_override_summary_flag(self):
        candidate = run("contradiction", {"x": .5}, 0, feasible=True, target=True)
        candidate["analysis"]["constraints"] = {"power": {"passed": False}}
        proposal = propose_bayesian(spec(), [candidate], {})
        self.assertEqual(proposal["action"], "submit")
        self.assertEqual(proposal["penalized_run_ids"], ["contradiction"])

    def test_two_consecutive_invalid_outcomes_stop_and_valid_feedback_resets(self):
        bad1 = run("bad1", {"x": .1}, 1, usable=False)
        bad2 = run("bad2", {"x": .2}, 1, usable=False)
        stopped = propose_bayesian(spec(), [bad1, bad2], {})
        self.assertEqual(stopped["action"], "stop")
        self.assertEqual(stopped["stop_code"], "repeated_invalid")
        resumed = propose_bayesian(spec(), [bad1, run("good", {"x": .6}, 8), bad2], {})
        self.assertEqual(resumed["action"], "submit")

    def test_target_hit_stops_as_candidate_without_mesh_promotion(self):
        candidate = run("hit", {"x": .3}, .5, target=True)
        proposal = propose_bayesian(spec(), [candidate], {})
        self.assertEqual(proposal["stop_code"], "target_requires_validation")
        self.assertTrue(proposal["requires_validation"])
        self.assertTrue(proposal["candidate_only"])
        self.assertNotIn("mesh_convergence", proposal)
        self.assertEqual(candidate["analysis"]["scientific_status"]["mesh_convergence"], "unknown")

    def test_old_objective_feedback_is_not_trained_or_used_to_stop(self):
        previous = run("old-hit", {"x": .5}, 0, target=True)
        previous["spec"] = spec()
        previous["spec"]["objective"]["target_phase_deg"] = 30
        proposal = propose_bayesian(spec(), [previous], {})
        self.assertEqual(proposal["action"], "submit")
        self.assertEqual(proposal["training_run_ids"], [])
        self.assertEqual(proposal["excluded_runs"][0]["reason"], "incompatible_experiment")

    def test_best_target_candidate_is_selected_from_actual_valid_scores(self):
        proposal = propose_bayesian(spec(), [run("better", {"x": .3}, .2, target=True),
                                             run("latest", {"x": .4}, .8, target=True)], {})
        self.assertEqual(proposal["best_run_id"], "better")
        self.assertEqual(proposal["best_phase_error_deg"], .2)

    def test_active_run_waits(self):
        active = run("active", {"x": .5}, 1)
        active["state"] = "solving"
        self.assertEqual(propose_bayesian(spec(), [active], {})["action"], "wait")

    def test_multidimensional_gp_returns_all_dimensions_inside_bounds(self):
        contract = spec({f"x{i}": [-i - 1, i + 2] for i in range(4)})
        rng = np.random.default_rng(51)
        runs = []
        for index in range(14):
            normalized = rng.random(4)
            point = {f"x{i}": float(-i - 1 + (2 * i + 3) * normalized[i]) for i in range(4)}
            error = float(10 + 20 * np.sum((normalized - .3) ** 2))
            runs.append(run(str(index), point, error))
        proposal = propose_bayesian(contract, runs, {"seed": 51, "initial_samples": 5})
        self.assertEqual(proposal["stage"], "bayesian_optimization")
        self.assertEqual(set(proposal["parameters"]), set(contract["parameter_bounds"]))
        for name, value in proposal["parameters"].items():
            low, high = contract["parameter_bounds"][name]
            self.assertTrue(low <= value <= high)
        self.assertNotIn(proposal["parameters"], [item["job"]["parameters"] for item in runs])

    def test_no_duplicate_after_rounding_in_an_exhausted_float_interval(self):
        low, high = 1.0, math.nextafter(1.0, 2.0)
        runs = [run("low", {"x": low}, 2), run("high", {"x": high}, 3)]
        proposal = propose_bayesian(spec({"x": [low, high]}), runs, {"initial_samples": 2})
        self.assertEqual(proposal["stop_code"], "candidate_pool_exhausted")

    def test_malformed_config_and_bounds_fail_before_proposal(self):
        bad_configs = [{"seed": True}, {"seed": -1}, {"candidate_count": 64.0},
                       {"candidate_count": float("inf")}, {"initial_samples": 1},
                       {"seed": "5"}, {"unsupported": 1}]
        for config in bad_configs:
            with self.subTest(config=config), self.assertRaises(ValueError):
                propose_bayesian(spec(), [], config)
        bad_bounds = [{"x": [False, 1]}, {"x": [0, float("nan")]}, {"x": [2, 1]},
                      {"x": [0, float("inf")]}, {"x": ["0", 1]}, {},
                      {f"x{i}": [0, 1] for i in range(9)}]
        for bounds in bad_bounds:
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                contract = spec()
                contract["parameter_bounds"] = bounds
                propose_bayesian(contract, [], {})

    def test_boolean_observation_cannot_be_trained(self):
        proposal = propose_bayesian(spec(), [run("bool", {"x": .5}, True)], {})
        self.assertEqual(proposal["training_run_ids"], [])
        self.assertEqual(proposal["excluded_runs"][0]["reason"], "invalid_or_missing_score")


if __name__ == "__main__":
    unittest.main()
