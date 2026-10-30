"""Diff 解析测试（PRD §4.2/§4.3）：行号映射、变更类型、噪音过滤。"""
from pr_reviewer.diff_extractor import parse_unified_diff, skip_reason_for

SAMPLE_DIFF = """\
diff --git a/app/auth.py b/app/auth.py
index 111..222 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,4 +10,6 @@ def login():
     ctx = get_ctx()
-    token = "old"
+    token = os.environ["TOKEN"]
+    audit(token)
     return ctx
diff --git a/package-lock.json b/package-lock.json
new file mode 100644
--- /dev/null
+++ b/package-lock.json
@@ -0,0 +1,3 @@
+{"locked": true}
"""


def test_new_file_line_mapping():
    result = parse_unified_diff(SAMPLE_DIFF)
    auth = next(f for f in result.files if f.file_path == "app/auth.py")
    # hunk 起始于新文件第 10 行：上下文10 + 删除1 + 新增2 → 变更行为 11、12
    assert auth.change_type == "modified"
    assert auth.changed_lines == [11, 12]
    assert 10 not in auth.changed_lines  # 上下文行不算变更


def test_lockfile_skipped_and_added_detected():
    result = parse_unified_diff(SAMPLE_DIFF)
    lock = next(f for f in result.files if f.file_path == "package-lock.json")
    assert lock.skip_reason is not None
    assert lock.change_type == "added"
    assert result.total_changed_lines == 3  # auth 2 行 + lock 1 行新增


def test_skip_patterns():
    assert skip_reason_for("vendor/lib/foo.py") is not None
    assert skip_reason_for("app/logo.png") is not None
    assert skip_reason_for("app/service.py") is None
