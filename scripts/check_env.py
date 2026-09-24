# -*- coding: utf-8 -*-
"""环境依赖自检脚本：列出关键依赖的安装状态。"""

import importlib.util
import sys

REQUIRED = [
    ("duckdb", "存储引擎"),
    ("pandas", "数据处理"),
    ("numpy", "数值计算"),
    ("pyarrow", "Parquet 读写"),
    ("requests", "HTTP"),
    ("yaml", "配置解析"),
    ("dotenv", "环境变量"),
    ("pydantic_settings", "配置模型"),
    ("baostock", "主数据源"),
    ("fastapi", "本地 API"),
    ("uvicorn", "ASGI 服务"),
    ("openai", "DeepSeek 客户端"),
    ("tenacity", "重试"),
    ("tqdm", "进度条"),
]

OPTIONAL = [
    ("akshare", "备数据源"),
    ("adata", "资金/概念/财务补充源"),
]


def check(mods):
    ok, missing = [], []
    for name, desc in mods:
        if importlib.util.find_spec(name):
            ok.append(name)
        else:
            missing.append(f"{name} ({desc})")
    return ok, missing


def main() -> int:
    print(f"Python: {sys.version.split()[0]}  |  {sys.executable}")
    ok, missing = check(REQUIRED)
    print(f"必需依赖 OK: {len(ok)}/{len(REQUIRED)}")
    if missing:
        print("  缺失必需依赖:")
        for m in missing:
            print(f"    - {m}")

    ok2, missing2 = check(OPTIONAL)
    if missing2:
        print("  缺失可选依赖 (不影响核心功能):")
        for m in missing2:
            print(f"    - {m}")
    else:
        print(f"可选依赖 OK: {len(ok2)}/{len(OPTIONAL)}")

    if missing:
        print("\n结论: 环境不完整，请重跑 scripts\\setup_env.bat")
        return 1
    print("\n结论: 核心环境就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
