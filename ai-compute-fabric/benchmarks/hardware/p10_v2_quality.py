#!/usr/bin/env python3
"""Pha V2.4 — Kiểm tra chất lượng đầu ra giữa llama.cpp (GGUF Q4_K_M) và vLLM (AWQ 4-bit).

Chạy 15 bài toán lập trình Python (trích từ bộ HumanEval chuẩn trong repo) với temperature=0,
chấm bằng unit test thực tế để bảo đảm lượng tử hóa AWQ / GGUF Q4_K_M không sinh rác.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


PROBLEMS_15: list[dict[str, str]] = [
    {
        "task_id": "HumanEval/0",
        "prompt": "from typing import List\n\ndef has_close_elements(numbers: List[float], threshold: float) -> bool:\n    \"\"\" Check if in given list of numbers, are any two numbers closer to each other than\n    given threshold.\n    >>> has_close_elements([1.0, 2.0, 3.0], 0.5)\n    False\n    >>> has_close_elements([1.0, 2.8, 3.0, 4.0, 5.0, 2.0], 0.3)\n    True\n    \"\"\"\n",
        "test": "assert has_close_elements([1.0, 2.0, 3.9, 4.0, 5.0, 2.2], 0.3) == True\nassert has_close_elements([1.0, 2.0, 3.9, 4.0, 5.0, 2.2], 0.05) == False\nassert has_close_elements([1.0, 2.0, 5.9, 4.0, 5.0], 0.95) == True\nassert has_close_elements([1.0, 2.0, 5.9, 4.0, 5.0], 0.8) == False\nassert has_close_elements([1.0, 2.0, 3.0, 4.0, 5.0, 2.0], 0.1) == True\n",
    },
    {
        "task_id": "HumanEval/2",
        "prompt": "def truncate_number(number: float) -> float:\n    \"\"\" Given a positive floating point number, it can be decomposed into\n    and integer part (largest integer smaller than given number) and decimals\n    (leftover part always smaller than 1).\n    Return the decimal part of the number.\n    >>> truncate_number(3.5)\n    0.5\n    \"\"\"\n",
        "test": "assert truncate_number(3.5) == 0.5\nassert abs(truncate_number(1.33) - 0.33) < 1e-6\nassert abs(truncate_number(123.456) - 0.456) < 1e-6\n",
    },
    {
        "task_id": "HumanEval/4",
        "prompt": "from typing import List\n\ndef mean_absolute_deviation(numbers: List[float]) -> float:\n    \"\"\" For a given list of input numbers, calculate Mean Absolute Deviation\n    around the mean of this dataset.\n    >>> mean_absolute_deviation([1.0, 2.0, 3.0, 4.0])\n    1.0\n    \"\"\"\n",
        "test": "assert abs(mean_absolute_deviation([1.0, 2.0, 3.0]) - 2.0/3.0) < 1e-6\nassert abs(mean_absolute_deviation([1.0, 2.0, 3.0, 4.0]) - 1.0) < 1e-6\nassert abs(mean_absolute_deviation([1.0, 2.0, 3.0, 4.0, 5.0]) - 6.0/5.0) < 1e-6\n",
    },
    {
        "task_id": "HumanEval/7",
        "prompt": "from typing import List\n\ndef filter_by_substring(strings: List[str], substring: str) -> List[str]:\n    \"\"\" Filter an input list of strings only for ones that contain given substring\n    >>> filter_by_substring([], 'a')\n    []\n    >>> filter_by_substring(['abc', 'bacd', 'cde', 'array'], 'a')\n    ['abc', 'bacd', 'array']\n    \"\"\"\n",
        "test": "assert filter_by_substring([], 'john') == []\nassert filter_by_substring(['xxx', 'asd', 'xxy', 'john doe', 'xxxAAA', 'xxx'], 'xxx') == ['xxx', 'xxxAAA', 'xxx']\nassert filter_by_substring(['grunt', 'trumpet', 'prune', 'gruesome'], 'run') == ['grunt', 'prune']\n",
    },
    {
        "task_id": "HumanEval/8",
        "prompt": "from typing import List, Tuple\n\ndef sum_product(numbers: List[int]) -> Tuple[int, int]:\n    \"\"\" For a given list of integers, return a tuple consisting of a sum and a product of all the integers in a list.\n    Empty sum should be equal to 0 and empty product should be equal to 1.\n    >>> sum_product([])\n    (0, 1)\n    >>> sum_product([1, 2, 3, 4])\n    (10, 24)\n    \"\"\"\n",
        "test": "assert sum_product([]) == (0, 1)\nassert sum_product([1, 1, 1]) == (3, 1)\nassert sum_product([100, 0]) == (100, 0)\nassert sum_product([3, 5, 7]) == (15, 105)\n",
    },
    {
        "task_id": "HumanEval/13",
        "prompt": "def greatest_common_divisor(a: int, b: int) -> int:\n    \"\"\" Return a greatest common divisor of two integers a and b\n    >>> greatest_common_divisor(3, 5)\n    1\n    >>> greatest_common_divisor(25, 15)\n    5\n    \"\"\"\n",
        "test": "assert greatest_common_divisor(3, 7) == 1\nassert greatest_common_divisor(10, 15) == 5\nassert greatest_common_divisor(49, 14) == 7\nassert greatest_common_divisor(144, 60) == 12\n",
    },
    {
        "task_id": "HumanEval/16",
        "prompt": "def count_distinct_characters(string: str) -> int:\n    \"\"\" Given a string, find out how many distinct characters (regardless of case) does it consist of\n    >>> count_distinct_characters('xyzXYZ')\n    3\n    >>> count_distinct_characters('Jerry')\n    4\n    \"\"\"\n",
        "test": "assert count_distinct_characters('') == 0\nassert count_distinct_characters('abcde') == 5\nassert count_distinct_characters('abcdeCADE') == 5\nassert count_distinct_characters('aaaaAAAAaaaa') == 1\nassert count_distinct_characters('Jerry jERRY JeRRRY') == 5\n",
    },
    {
        "task_id": "HumanEval/22",
        "prompt": "from typing import List, Any\n\ndef filter_integers(values: List[Any]) -> List[int]:\n    \"\"\" Filter given list of any python values only for integers\n    >>> filter_integers(['a', 3.14, 5])\n    [5]\n    >>> filter_integers([1, 2, 3, 'abc', {}, []])\n    [1, 2, 3]\n    \"\"\"\n",
        "test": "assert filter_integers([]) == []\nassert filter_integers([4, {}, [], 23.2, 9, 'adasd']) == [4, 9]\nassert filter_integers([3, 'c', 3, 3, 'a', 'b']) == [3, 3, 3]\n",
    },
    {
        "task_id": "HumanEval/23",
        "prompt": "def strlen(string: str) -> int:\n    \"\"\" Return length of given string\n    >>> strlen('')\n    0\n    >>> strlen('abc')\n    3\n    \"\"\"\n",
        "test": "assert strlen('') == 0\nassert strlen('x') == 1\nassert strlen('asdasnakj') == 9\n",
    },
    {
        "task_id": "HumanEval/27",
        "prompt": "def flip_case(string: str) -> str:\n    \"\"\" For a given string, flip lowercase characters to uppercase and uppercase to lowercase.\n    >>> flip_case('Hello')\n    'hELLO'\n    \"\"\"\n",
        "test": "assert flip_case('') == ''\nassert flip_case('Hello!') == 'hELLO!'\nassert flip_case('These violent delights have violent ends') == 'tHESE VIOLENT DELIGHTS HAVE VIOLENT ENDS'\n",
    },
    {
        "task_id": "HumanEval/28",
        "prompt": "from typing import List\n\ndef concatenate(strings: List[str]) -> str:\n    \"\"\" Concatenate list of strings into a single string\n    >>> concatenate([])\n    ''\n    >>> concatenate(['a', 'b', 'c'])\n    'abc'\n    \"\"\"\n",
        "test": "assert concatenate([]) == ''\nassert concatenate(['x', 'y', 'z']) == 'xyz'\nassert concatenate(['x', 'y', 'z', 'w', 'k']) == 'xyzwk'\n",
    },
    {
        "task_id": "HumanEval/31",
        "prompt": "def is_prime(n: int) -> bool:\n    \"\"\"Return true if a given number is prime, and false otherwise.\n    >>> is_prime(6)\n    False\n    >>> is_prime(101)\n    True\n    >>> is_prime(11)\n    True\n    >>> is_prime(1)\n    False\n    \"\"\"\n",
        "test": "assert is_prime(6) == False\nassert is_prime(101) == True\nassert is_prime(11) == True\nassert is_prime(13441) == True\nassert is_prime(61) == True\nassert is_prime(4) == False\nassert is_prime(1) == False\n",
    },
    {
        "task_id": "HumanEval/34",
        "prompt": "from typing import List\n\ndef unique(l: List[int]) -> List[int]:\n    \"\"\"Return sorted unique elements in a list\n    >>> unique([5, 3, 5, 2, 3, 3, 9, 0, 123])\n    [0, 2, 3, 5, 9, 123]\n    \"\"\"\n",
        "test": "assert unique([5, 3, 5, 2, 3, 3, 9, 0, 123]) == [0, 2, 3, 5, 9, 123]\n",
    },
    {
        "task_id": "HumanEval/35",
        "prompt": "from typing import List\n\ndef max_element(l: List[int]) -> int:\n    \"\"\"Return maximum element in the list.\n    >>> max_element([1, 2, 3])\n    3\n    >>> max_element([5, 3, -5, 2, -3, 3, 9, 0, 123, 1, -10])\n    123\n    \"\"\"\n",
        "test": "assert max_element([1, 2, 3]) == 3\nassert max_element([5, 3, -5, 2, -3, 3, 9, 0, 123, 1, -10]) == 123\n",
    },
    {
        "task_id": "HumanEval/45",
        "prompt": "def triangle_area(a: float, h: float) -> float:\n    \"\"\"Given length of a side and high return area for a triangle.\n    >>> triangle_area(5, 3)\n    7.5\n    \"\"\"\n",
        "test": "assert triangle_area(5, 3) == 7.5\nassert triangle_area(2, 2) == 2.0\nassert triangle_area(10, 8) == 40.0\n",
    },
]


def extract_python_code(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def run_quality_suite(base_url: str, model: str, engine_label: str) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    passed = 0
    details = []
    t0 = time.monotonic()

    for prob in PROBLEMS_15:
        user_msg = (
            "Complete the following Python function. Return ONLY valid Python code inside a ```python ... ``` block.\n\n"
            f"```python\n{prob['prompt']}```\n/no_think"
        )
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": user_msg}],
            "temperature": 0.0,
            "seed": 42,
            "max_tokens": 256,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        code = extract_python_code(content)
        full_script = f"{prob['prompt']}\n{code}\n\n{prob['test']}\n"

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(full_script)
            tf_path = tf.name

        proc = subprocess.run([sys.executable, tf_path], capture_output=True, text=True, timeout=10)
        Path(tf_path).unlink(missing_ok=True)
        ok = proc.returncode == 0
        if ok:
            passed += 1
        details.append({
            "task_id": prob["task_id"],
            "passed": ok,
            "stderr": proc.stderr[-200:] if not ok else "",
        })

    return {
        "engine_label": engine_label,
        "total": len(PROBLEMS_15),
        "passed": passed,
        "pass_rate": round(passed / len(PROBLEMS_15), 4),
        "elapsed_s": round(time.monotonic() - t0, 2),
        "details": details,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", default="aicf")
    ap.add_argument("--engine-label", required=True)
    ap.add_argument("--out-jsonl", default="")
    args = ap.parse_args()

    res = run_quality_suite(args.base_url, args.model, args.engine_label)
    print(json.dumps(res, ensure_ascii=False))
    if args.out_jsonl:
        with open(args.out_jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
