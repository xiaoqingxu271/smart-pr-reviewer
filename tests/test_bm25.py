"""BM25 与 RRF 的行为测试（评测用支路，不接生产链路）。

重点盯三件容易悄悄出错的事：驼峰切分必须在原文上做、同分排序必须稳定（否则两次跑
结果不一样）、以及 RRF 只用名次不用分数（两路分数不可比是这个项目已经踩过的坑）。
"""
from pr_reviewer.bm25 import Bm25Index, rrf, tokenize


def test_tokenize_splits_camel_case_and_keeps_cjk_bigrams():
    assert tokenize("HTTPServer") == ["http", "server"]
    assert tokenize("sqlite3execute") == ["sqlite", "3", "execute"]
    assert tokenize("subprocess.run(cmd, shell=True)")[:6] == [
        "subprocess", "run", "cmd", "shell", "true"]
    toks = tokenize("禁止硬编码密钥")
    assert "禁" in toks and "禁止" in toks and "密钥" in toks


def test_tokenize_keeps_all_caps_acronyms():
    """`API_KEY` 与 `MD5` 不能丢：它们正是硬编码密钥/弱哈希条款最硬的词面信号。

    第一版正则只处理"缩写前缀 + 单词 + 数字"三类，实测把 API_KEY 切成只剩 demo，
    MD5 只剩 5——词法支路当场废掉一半，而且不会报错。
    """
    assert tokenize('API_KEY = "demo"') == ["api", "key", "demo"]
    assert tokenize("MD5") == ["md", "5"]
    assert "password" in tokenize("PASSWORD_HASH = hash(password)")


def test_bm25_prefers_the_rare_matching_term():
    """命中稀有词的文档应压过只命中常见词的文档——这是词法检索的全部价值所在。"""
    idx = Bm25Index()
    idx.add("common", "the code uses a function and a variable and a return value")
    idx.add("rare", "the code calls md5 to hash the password")
    idx.add("other", "nothing relevant here at all")
    ranked = idx.search("md5 hash", top_k=3)
    assert [d for d, _ in ranked] == ["rare"], "没有共同词的文档不该被召回"

    both = idx.search("md5 code", top_k=3)
    assert next(d for d, _ in both) == "rare"          # 稀有词命中 > 常见词命中


def test_bm25_is_deterministic_on_ties():
    idx = Bm25Index()
    for i in range(5):
        idx.add(f"SEC-{i:03d}", "password hashing")
    first = idx.search("password")
    assert [d for d, _ in first] == ["SEC-000", "SEC-001", "SEC-002", "SEC-003", "SEC-004"]
    assert first == idx.search("password")            # 同分按入库顺序，不随哈希种子变


def test_empty_index_and_no_overlap():
    idx = Bm25Index()
    assert idx.search("anything") == []
    idx.add("A", "alpha")
    assert idx.search("zzz") == []


def test_rrf_uses_ranks_not_scores():
    """两路分数尺度完全不同（余弦 vs BM25 原始分），RRF 只认名次。"""
    dense = ["a", "b", "c"]
    lexical = ["c", "a", "b"]
    assert rrf([dense, lexical], k=60, top_n=3) == ["a", "c", "b"]
    # 一路独占头部也能赢：k 越小越看重头部名次
    assert rrf([["x", "y"], ["z", "w"]], k=1, top_n=1) == ["x"]
