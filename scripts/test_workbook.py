"""Offline regression coverage for saved-workbook deployment."""
import copy
import json
import unittest

import build_workbook as builder
from validate_queries import structure_check, walk


class WorkbookDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = builder.load()
        cls.workbook = builder.build(cls.data)

    def test_saved_workbook_and_arm_resource_fit_size_limit(self):
        resource = builder.arm_template(self.workbook)["resources"][0]
        self.assertLess(len(resource["properties"]["serializedData"].encode("utf-8")),
                        builder.WORKBOOK_SIZE_LIMIT)
        self.assertLess(len(json.dumps(resource).encode("utf-8")),
                        builder.WORKBOOK_SIZE_LIMIT)
        self.assertEqual(json.loads(resource["properties"]["serializedData"]), self.workbook)
        self.assertEqual(resource["properties"]["version"], self.workbook["version"])

    def test_shared_parameters_preserve_complete_lookup(self):
        lookups = builder.lookup_values(self.workbook)
        sku_map, _, _, regions, region_ids = builder.build_lookups(self.data)
        for name, value in zip(builder.LOOKUP_PARAMETERS, (sku_map, regions, region_ids)):
            self.assertEqual(lookups[name], builder.kql_bag(value))
        parameters = [
            p for item in self.workbook["items"] if item.get("type") == 9
            for p in item["content"]["parameters"]
            if p["name"] in builder.LOOKUP_PARAMETERS
        ]
        self.assertEqual(len(parameters), 3)
        for parameter in parameters:
            self.assertEqual(parameter["type"], 1)
            self.assertTrue(parameter["isHiddenWhenLocked"])
            self.assertTrue(parameter["isRequired"])
            self.assertNotIn("query", parameter)

    def test_query_expansion_preserves_original_inventory_and_scoring(self):
        sku_map, _, _, regions, region_ids = builder.build_lookups(self.data)
        original = builder.base_query(sku_map, regions, region_ids)
        queries = []
        walk(self.workbook, queries)
        self.assertEqual(len(queries), 15)
        for name, query in queries:
            expanded = builder.expand_lookups(query, self.workbook)
            if name == "about-allsizes":
                self.assertEqual(expanded, query)
            else:
                self.assertTrue(expanded.startswith(original), name)
                for parameter in builder.LOOKUP_PARAMETERS:
                    self.assertNotIn("{" + parameter + "}", expanded)

    def test_structure_is_valid(self):
        self.assertEqual(structure_check(self.workbook), [])

    def test_missing_shared_parameter_is_rejected(self):
        workbook = copy.deepcopy(self.workbook)
        item = next(i for i in workbook["items"] if i["name"] == "lookup-parameters")
        item["content"]["parameters"].pop()
        self.assertIn("missing shared lookup RegionIdMap", structure_check(workbook))

    def test_oversized_workbook_cannot_generate_deployment_template(self):
        with self.assertRaisesRegex(ValueError, "size limit"):
            builder.arm_template({"version": "Notebook/1.0",
                                  "items": [], "padding": "x" * builder.WORKBOOK_SIZE_LIMIT})


if __name__ == "__main__":
    unittest.main()
