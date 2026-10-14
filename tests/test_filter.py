"""降误报后处理测试：类型白名单 + 文件级评分 + 置信度闸 + 语义聚类去重。"""

from __future__ import annotations

import unittest

from core.filter import (
    DROP_COMMENT,
    DROP_FORMAT,
    DROP_NAMING,
    DROP_REFACTOR,
    DROP_STYLE,
    TYPE_NULL_POINTER,
    TYPE_OUT_OF_BOUNDS,
    TYPE_PATH_TRAVERSAL,
    TYPE_RESOURCE_LEAK,
    TYPE_SQL_INJECTION,
    TYPE_XSS,
    bigram_similarity,
    changed_line_count,
    classify_type,
    cluster_findings,
    filter_by_confidence,
    filter_by_type,
    filter_files_by_score,
    has_sensitive_operation,
    is_production_path,
    score_changeset,
    should_review_file,
)
from findings.schema import ChangeSet, Finding, Hunk, HunkLine, normalize_confidence


def raw(message: str, confidence, file: str = "a.py") -> dict:
    return {
        "file": file,
        "existing_code": "x = 1",
        "message": message,
        "severity": "high",
        "category": "bug",
        "confidence": confidence,
    }


def f(file: str, start: int, end: int, message: str = "m", confidence: float = 0.9) -> Finding:
    return Finding(
        file=file,
        start_line=start,
        end_line=end,
        message=message,
        severity="high",
        category="bug",
        confidence=confidence,
    )


class NormalizeConfidenceTest(unittest.TestCase):
    def test_accepts_float_string_percent_and_junk(self):
        self.assertEqual(normalize_confidence(0.9), 0.9)
        self.assertEqual(normalize_confidence("0.75"), 0.75)
        self.assertEqual(normalize_confidence("85%"), 0.85)
        self.assertEqual(normalize_confidence(90), 0.9)
        self.assertEqual(normalize_confidence(200), 1.0)
        self.assertEqual(normalize_confidence(-1), 0.0)
        self.assertEqual(normalize_confidence(None), 0.5)   # 缺失 -> 待复核
        self.assertEqual(normalize_confidence(True), 0.5)   # 布尔不算数
        self.assertEqual(normalize_confidence("高"), 0.5)


class ConfidenceGateTest(unittest.TestCase):
    def test_three_buckets(self):
        kept, stats = filter_by_confidence(
            [raw("高把握", 0.9), raw("中等把握", 0.65), raw("瞎猜", 0.3)]
        )
        self.assertEqual([item["message"] for item in kept], ["高把握", "中等把握"])
        self.assertEqual(stats.dropped_low_confidence, 1)
        self.assertEqual(stats.needs_review, 1)
        self.assertEqual(stats.kept, 2)
        self.assertFalse(kept[0]["needs_review"])
        self.assertTrue(kept[1]["needs_review"])

    def test_boundaries_are_inclusive_on_the_keep_side(self):
        kept, stats = filter_by_confidence([raw("正好 0.8", 0.8), raw("正好 0.5", 0.5)])
        self.assertEqual(stats.dropped_low_confidence, 0)
        self.assertEqual(stats.needs_review, 1)
        self.assertFalse(kept[0]["needs_review"])

    def test_input_dicts_are_not_mutated(self):
        item = raw("x", 0.6)
        filter_by_confidence([item])
        self.assertNotIn("needs_review", item)


def hunk(lines, new_start: int = 1) -> Hunk:
    """把 ("+"|"-"|" ", content) 列表包成一个 hunk，并补上新侧行号。"""
    items = []
    number = new_start
    for kind, content in lines:
        new_line = None if kind == "-" else number
        items.append(HunkLine(type=kind, content=content, new_line=new_line))
        if kind != "-":
            number += 1
    return Hunk(
        old_start=new_start,
        old_lines=len(items),
        new_start=new_start,
        new_lines=len(items),
        lines=items,
    )


def changeset(path: str, lines, old: str = "", new: str = "") -> ChangeSet:
    hs = [hunk(lines)] if lines else []
    return ChangeSet(file=path, head_start=1, head_end=len(lines), hunks=hs,
                     old_content=old, new_content=new)


class TypeWhitelistClassifyTest(unittest.TestCase):
    """类型白名单的归类：六类"必报"要认出来，"不报"那一栏也要认出来。"""

    def test_required_types_are_detected_from_real_messages(self):
        cases = [
            (
                "查询语句用字符串拼接用户输入，存在 SQL 注入",
                TYPE_SQL_INJECTION,
            ),
            (
                "workspaceId 为绝对路径时 path.resolve 会直接返回该路径，startsWith 校验被绕过",
                TYPE_PATH_TRAVERSAL,
            ),
            (
                "dangerouslySetInnerHTML 直接吃用户输入，未转义，存在 XSS",
                TYPE_XSS,
            ),
            (
                "msg.settings 未做空值校验即解构，收到缺失字段会抛 TypeError",
                TYPE_NULL_POINTER,
            ),
            (
                "activePaletteIndex 越界时未处理，可能访问 palettes 之外的下标",
                TYPE_OUT_OF_BOUNDS,
            ),
            (
                "快照文件没有任何删除逻辑，长期运行造成存储泄漏",
                TYPE_RESOURCE_LEAK,
            ),
            (
                "文件句柄 open 之后没有 try/finally，异常路径下不会 close",
                TYPE_RESOURCE_LEAK,
            ),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(classify_type(message, "bug"), expected)

    def test_not_reported_categories(self):
        cases = [
            ("变量命名不一致，建议改成 camelCase", DROP_NAMING),
            ("缺少注释，文档字符串没写清楚参数", DROP_COMMENT),
            ("CSS 颜色值硬编码，主题对比度不足", DROP_STYLE),
            ("缩进和格式不统一，应该跑一下 prettier", DROP_FORMAT),
            ("这段代码可以重构，重复代码建议提取函数", DROP_REFACTOR),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(classify_type(message, "maintainability"), expected)

    def test_unclassified_returns_none(self):
        self.assertIsNone(classify_type("防抖写入 effect 中三次 await 串行执行且没有 try/catch", "bug"))
        self.assertIsNone(classify_type("", None))


class TypeWhitelistGateTest(unittest.TestCase):
    def _items(self):
        return [
            raw("SQL 注入：拼接用户输入到查询", 0.9),
            raw("命名不一致，建议改一下", 0.9),
            raw("CSS 对比度不足", 0.9),
            raw("这段可以重构，建议提取函数", 0.9),
            raw("并发竞态导致状态不一致", 0.9),
        ]

    def test_blacklist_mode_drops_only_the_do_not_report_bucket(self):
        kept, stats = filter_by_type(self._items(), mode="blacklist")
        self.assertEqual([item["message"] for item in kept], [
            "SQL 注入：拼接用户输入到查询",
            "并发竞态导致状态不一致",
        ])
        self.assertEqual(stats.dropped, 3)
        self.assertEqual(stats.per_type[DROP_NAMING], 1)
        self.assertEqual(stats.per_type[DROP_REFACTOR], 1)

    def test_strict_mode_keeps_only_required_types(self):
        kept, stats = filter_by_type(self._items(), mode="strict")
        self.assertEqual([item["message"] for item in kept], ["SQL 注入：拼接用户输入到查询"])
        self.assertEqual(stats.dropped, 4)
        self.assertEqual(stats.per_type["unclassified"], 1)

    def test_input_is_not_mutated(self):
        items = self._items()
        filter_by_type(items, mode="blacklist")
        self.assertEqual(len(items), 5)


class FileScoreTest(unittest.TestCase):
    def test_production_path(self):
        self.assertTrue(is_production_path("src/app/main.py"))
        self.assertTrue(is_production_path("server/index.js"))
        self.assertTrue(is_production_path(".github/workflows/publish.yml"))
        self.assertTrue(is_production_path("Dockerfile"))
        # 测试 / 文档 / 样式 / 依赖清单 / 生成物 / 非代码文件都不算 production
        self.assertFalse(is_production_path("tests/test_app.py"))
        self.assertFalse(is_production_path("src/components/App.css"))
        self.assertFalse(is_production_path("README.md"))
        self.assertFalse(is_production_path("package.json"))
        self.assertFalse(is_production_path("dist/bundle.min.js"))

    def test_changed_line_count_counts_added_and_removed(self):
        cs = changeset("src/a.py", [(" ", "ctx"), ("+", "new"), ("-", "old")])
        self.assertEqual(changed_line_count(cs), 2)

    def test_sensitive_operation(self):
        self.assertTrue(has_sensitive_operation(changeset("src/a.py", [("+", "cur.execute(sql)")])))
        self.assertTrue(has_sensitive_operation(changeset("src/a.py", [("+", "await fetch(url)")])))
        self.assertTrue(has_sensitive_operation(changeset("src/a.py", [("+", "await fs.promises.open(path)")])))
        self.assertFalse(has_sensitive_operation(changeset("src/a.py", [("+", "total = a + b")])))
        # 删除行也算
        self.assertTrue(has_sensitive_operation(changeset("src/a.py", [("-", "conn.close()")])))

    def test_score_is_the_sum_of_three_signals(self):
        # production + 改动 > 5 行 + 敏感操作 = 1.0
        cs = changeset("src/a.py", [("+", f"query = f'select {i}'") for i in range(6)])
        score = score_changeset(cs)
        self.assertAlmostEqual(score.score, 1.0)
        self.assertEqual(len(score.reasons), 3)

    def test_score_production_only(self):
        cs = changeset("src/a.py", [("+", "total = a + b")])
        self.assertAlmostEqual(score_changeset(cs).score, 0.4)
        self.assertFalse(should_review_file(cs))

    def test_score_production_plus_sensitive_without_large_change(self):
        cs = changeset("src/a.py", [("+", "cur.execute(sql)")])
        self.assertAlmostEqual(score_changeset(cs).score, 0.7)
        self.assertTrue(should_review_file(cs))

    def test_non_code_file_loses_the_production_point(self):
        # 非代码/配置文件拿不到 production 那 0.4 分——同样大小同样敏感，分数更低
        non_code = changeset("assets/data/seed.txt", [("+", "select * from users") for _ in range(10)])
        code = changeset("src/a.py", [("+", "select * from users") for _ in range(10)])
        self.assertLess(score_changeset(non_code).score, score_changeset(code).score)
        self.assertAlmostEqual(score_changeset(non_code).score, 0.6)
        self.assertAlmostEqual(score_changeset(code).score, 1.0)

    def test_manifest_and_style_files_are_below_threshold(self):
        manifest = changeset("package.json", [("+", "\"name\": \"x\"") for _ in range(10)])
        self.assertFalse(should_review_file(manifest))

    def test_style_file_is_below_threshold(self):
        cs = changeset("src/App.css", [("+", ".a { color: red }") for _ in range(20)])
        self.assertFalse(should_review_file(cs))

    def test_threshold_is_configurable(self):
        cs = changeset("src/a.py", [("+", "total = a + b")])
        self.assertFalse(should_review_file(cs, threshold=0.5))
        self.assertTrue(should_review_file(cs, threshold=0.4))

    def test_filter_files_by_score_drops_files_and_their_findings(self):
        keep = changeset("src/a.py", [("+", "cur.execute(sql)")])
        drop = changeset("docs/notes.md", [("+", "hello")])
        findings = [
            {"file": "src/a.py", "message": "m1"},
            {"file": "docs/notes.md", "message": "m2"},
        ]
        kept_cs, kept_findings, stats = filter_files_by_score([keep, drop], findings)
        self.assertEqual([item.file for item in kept_cs], ["src/a.py"])
        self.assertEqual([item["message"] for item in kept_findings], ["m1"])
        self.assertEqual((stats.skipped_files, stats.skipped_findings), (1, 1))
        self.assertEqual(stats.skipped_detail[0]["file"], "docs/notes.md")


class BigramSimilarityTest(unittest.TestCase):
    def test_identical_and_unrelated(self):
        self.assertEqual(bigram_similarity("缺少长度校验", "缺少长度校验"), 1.0)
        self.assertLess(bigram_similarity("缺少长度校验", "数据库连接未关闭"), 0.6)
        self.assertEqual(bigram_similarity("", "x"), 0.0)


class ClusterTest(unittest.TestCase):
    def test_similar_messages_same_file_merge_to_one(self):
        findings = [
            f("a.py", 10, 14, "未对 code 参数做长度校验，存在 ReDoS 风险"),
            f("a.py", 12, 12, "未对 code 参数做长度校验，存在 ReDoS 风险。"),
        ]
        kept, stats = cluster_findings(findings)
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats.merged, 1)
        self.assertEqual(stats.clusters, 1)
        # 行区间最窄的那条胜出（行号最精确）
        self.assertEqual((kept[0].start_line, kept[0].end_line), (12, 12))

    def test_same_span_prefers_longer_message(self):
        findings = [
            f("a.py", 10, 10, "code 参数缺少长度校验"),
            f("a.py", 10, 10, "code 参数缺少长度校验，可被 ReDoS 拖死"),
        ]
        kept, _ = cluster_findings(findings)
        self.assertEqual(len(kept), 1)
        self.assertIn("ReDoS", kept[0].message)

    def test_same_message_different_file_does_not_merge(self):
        findings = [f("a.py", 10, 10, "缺少长度校验"), f("b.py", 10, 10, "缺少长度校验")]
        kept, stats = cluster_findings(findings)
        self.assertEqual((len(kept), stats.merged), (2, 0))

    def test_different_messages_same_file_do_not_merge(self):
        findings = [
            f("a.py", 10, 10, "未校验 code 参数长度，存在 ReDoS 风险"),
            f("a.py", 30, 30, "数据库连接未关闭，连接池会被耗尽"),
        ]
        kept, stats = cluster_findings(findings)
        self.assertEqual((len(kept), stats.merged), (2, 0))

    def test_transitive_closure(self):
        findings = [
            f("a.py", 10, 20, "未校验 code 参数长度，存在 ReDoS 风险"),
            f("a.py", 11, 19, "未校验 code 参数长度，存在 ReDoS 风险！"),
            f("a.py", 12, 18, "未校验 code 参数长度，存在 ReDoS 风险。"),
        ]
        kept, stats = cluster_findings(findings)
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats.merged, 2)
        self.assertEqual((kept[0].start_line, kept[0].end_line), (12, 18))

    def test_empty_and_single(self):
        self.assertEqual(cluster_findings([]), ([], cluster_findings([])[1]))
        kept, stats = cluster_findings([f("a.py", 1, 1)])
        self.assertEqual((len(kept), stats.merged), (1, 0))
        self.assertEqual(stats.input, 1)


if __name__ == "__main__":
    unittest.main()
