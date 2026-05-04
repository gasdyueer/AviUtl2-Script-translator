#!/usr/bin/env python3
"""
AviUtl2 脚本翻译解析器 & .aul2 生成器 & AI翻译  v2.1
=====================================================
用法:
  python aviutl2_l10n.py parse Script/              # 扫描解析所有脚本
  python aviutl2_l10n.py parse Script/ -n Basic_S    # 只看某个命名空间
  python aviutl2_l10n.py generate Script/ Language/  # 生成 zh.XXX.aul2 翻译模板
  python aviutl2_l10n.py generate Script/ Language/ --force  # 强制覆盖
  python aviutl2_l10n.py translate Language/ -k sk-xxx  # AI翻译 (DeepSeek)
  python aviutl2_l10n.py translate Language/ -k sk-xxx -n Basic_S --dry-run  # 预览
"""

import os
import re
import sys
import json
import time
import argparse
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# DeepSeek API (需要: pip install openai)
from openai import OpenAI


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExtractedItem:
    """从脚本注解中提取的一条可翻译文本"""
    category: str        # effect_name|track|select|select_option|check|
                         # color|file|group|value|param|label
    japanese: str        # 日文(或英文)原文
    namespace: str       # 所属命名空间
    effect_name: str = ""   # 所属效果名
    param_var: str = ""     # 参数变量名
    context: str = ""       # 额外上下文


# ═══════════════════════════════════════════════════════════════
# 正则模式 —— 兼容多种脚本写法
# ═══════════════════════════════════════════════════════════════

# --information: 的两种格式

RE_INFO_FULL  = re.compile(r'^--information:(\S+)@(\S+)')   # EffectName@Namespace ...
RE_INFO_PLAIN = re.compile(r'^--information:(\S+)')         # 描述文本 (无 @Namespace，效果名用文件名)

# 效果声明: @EffectName (AviUtl2 效果定义，效果名可以数字开头如 @2次式)
RE_EFFECT = re.compile(r'^@(\S+)')
RE_LABEL  = re.compile(r'^--label:(.+)$')

# ── 标准格式: --type@var:Display,... ──
ANNOTATION_STANDARD_2G = [
    ("track", re.compile(r'^--track@(\w+):([^,]+),')),
    ("check", re.compile(r'^--check@(\w+):([^,]+),')),
    ("color", re.compile(r'^--color@(\w+):([^,]+),')),
    ("file",  re.compile(r'^--file@(\w+):(.+)$')),
    ("value", re.compile(r'^--value@(\w+):([^,]+),')),
]
RE_SELECT = re.compile(r'^--select@(\w+):(.+)$')

# ── 旧式格式: --typeN:Display,... (无 @ 符号，关键词带数字后缀) ──
ANNOTATION_LEGACY_2G = [
    ("track", re.compile(r'^--track(\d+):([^,]+),')),
    ("check", re.compile(r'^--check(\d+):([^,]+),')),
]
# 旧式 select: --select@sN:Display=N,...
RE_SELECT_LEGACY = re.compile(r'^--select@([a-z]\d+):(.+)$')

# ── 单捕获组 (无 var_name) ──
ANNOTATION_1G = [
    ("group", re.compile(r'^--group:([^,]+),')),
    ("param", re.compile(r'^--param:([^,]+),')),
]

# 内部变量名，无需翻译
INTERNAL_NAMES = frozenset({"PI", "obj", "temp", "chm", "bkg", "mask", "geo"})

# DeepSeek API
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
TRANSLATE_BATCH_DELAY = 1.0  # 批次间隔秒数，避免触发限流


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def extract_select_options(rest: str) -> Tuple[str, List[str]]:
    """解析 select 右侧: Display=N,Opt1=v1,Opt2=v2,... → (显示名, [选项名])"""
    parts = rest.split(',')
    if not parts:
        return "", []
    first_eq = parts[0].find('=')
    display_name = parts[0][:first_eq] if first_eq >= 0 else parts[0]
    options = []
    for opt_part in parts[1:]:
        opt_part = opt_part.strip()
        if not opt_part:
            continue
        eq_idx = opt_part.find('=')
        options.append(opt_part[:eq_idx] if eq_idx > 0 else opt_part)
    return display_name, options


def should_skip(japanese: str) -> bool:
    """过滤内部/无需翻译的显示名"""
    jp = (japanese or "").strip()
    if not jp or jp in INTERNAL_NAMES:
        return True
    if re.match(r'^[0-9.\-+x#%°]+$', jp):
        return True
    return False


# 所有可翻译注解关键词（用于判断一行 -- 行是否属于可翻译注解）
_ANNOTATION_KEYWORDS = frozenset({
    '--information:', '--label:', '--track@', '--track',
    '--select@', '--check@', '--check',
    '--color@', '--file@', '--value@',
    '--group:', '--param:',
    '--filter', '--timecontrol',  # 虽然不是翻译内容，但是注解行
})


def _is_annotation_line(line: str) -> bool:
    """判断一行 -- 开头的行是否是注解（而非注释/代码）"""
    stripped = line.strip()
    # 跳过块注释 --[[ 和 --]
    if stripped.startswith('--[[') or stripped.startswith('--]'):
        return False
    # 跳过纯 Lua 注释（-- 后跟空格或 Lua 代码）
    if stripped.startswith('-- ') or stripped.startswith('--\t'):
        return False
    # 匹配已知注解关键词
    for kw in _ANNOTATION_KEYWORDS:
        if stripped.startswith(kw):
            return True
    return False


def derive_namespace(filepath: str) -> str:
    """从文件名推导命名空间（去掉扩展名和开头的 @）"""
    base = os.path.splitext(os.path.basename(filepath))[0]
    return base.lstrip('@')


# ═══════════════════════════════════════════════════════════════
# 核心解析器
# ═══════════════════════════════════════════════════════════════

def parse_script_file(filepath: str) -> List[ExtractedItem]:
    """解析单个脚本文件，返回所有可翻译条目"""
    results: List[ExtractedItem] = []
    fallback_ns = derive_namespace(filepath)      # 文件名推导的 namespace
    current_effect: Optional[str] = None
    namespace: str = fallback_ns                   # 当前 namespace (可能被 info 覆盖)
    found_info: bool = False                       # 是否匹配过 --information:

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except (IOError, UnicodeDecodeError) as e:
        print(f"  [警告] 无法读取 {filepath}: {e}", file=sys.stderr)
        return results

    for line in lines:
        line = line.rstrip('\n\r')

        # ── 非注解行：检测 @EffectName ──
        if not line.startswith('--'):
            m = RE_EFFECT.match(line)
            if m:
                current_effect = m.group(1).strip()
                found_info = False       # 新效果开始 → 重置上下文等待 --information: 或回退
            continue

        # ── --information:EffectName@Namespace ... ──
        m = RE_INFO_FULL.match(line)
        if m:
            current_effect = f"{m.group(1)}@{m.group(2)}"
            namespace = m.group(2)
            found_info = True
            results.append(ExtractedItem("effect_name", current_effect, namespace,
                                         effect_name=current_effect))
            continue

        # ── --information:描述文本 (无 @Namespace) ──
        m = RE_INFO_PLAIN.match(line)
        if m:
            if current_effect is None:
                current_effect = fallback_ns          # 无 @EffectName → 效果名=文件名
            else:
                current_effect = f"{current_effect}@{fallback_ns}"  # 有 @EffectName → 补 @Namespace
            namespace = fallback_ns
            found_info = True
            results.append(ExtractedItem("effect_name", current_effect, namespace,
                                         effect_name=current_effect))
            continue
        # ── --label:Category\Sub ──
        m = RE_LABEL.match(line)
        if m:
            label_jp = m.group(1)
            results.append(ExtractedItem("label", label_jp, namespace,
                                         effect_name=current_effect or ""))
            continue

        # ── 遇到首个可翻译注解但还没有效果上下文 ──
        # (仅在确实是一个注解行时才触发，跳过 --[[ 块注释和 -- Lua 注释)
        if not found_info and current_effect is not None:
            # @EffectName 来自非注释行，但没有 --information: — 构建 EffectName@Namespace
            if not _is_annotation_line(line):
                continue
            current_effect = f"{current_effect}@{namespace}"
            found_info = True
            results.append(ExtractedItem("effect_name", current_effect, namespace,
                                         effect_name=current_effect))
        elif not found_info and current_effect is None:
            if not _is_annotation_line(line):
                continue
            current_effect = fallback_ns
            namespace = fallback_ns
            found_info = True
            results.append(ExtractedItem("effect_name", current_effect, namespace,
                                         effect_name=current_effect))
        elif current_effect is None:
            continue

        # ── --select@var:Display=N,... (标准) ──
        m = RE_SELECT.match(line)
        if m:
            var_name, rest = m.group(1), m.group(2)
            disp_name, options = extract_select_options(rest)
            if not should_skip(disp_name):
                results.append(ExtractedItem("select", disp_name, namespace,
                                             current_effect, var_name))
            for opt in options:
                if not should_skip(opt):
                    results.append(ExtractedItem("select_option", opt, namespace,
                                                 current_effect, var_name, disp_name))
            continue

        # ── 标准双捕获组: track/check/color/file/value ──
        matched = False
        for cat, pattern in ANNOTATION_STANDARD_2G:
            m = pattern.match(line)
            if m:
                var_name, disp = m.group(1), m.group(2).strip()
                if not should_skip(disp):
                    results.append(ExtractedItem(cat, disp, namespace,
                                                 current_effect, var_name))
                matched = True
                break
        if matched:
            continue

        # ── 旧式双捕获组: trackN:/checkN: ──
        for cat, pattern in ANNOTATION_LEGACY_2G:
            m = pattern.match(line)
            if m:
                var_name = f"{cat}{m.group(1)}"
                disp = m.group(2).strip()
                if not should_skip(disp):
                    results.append(ExtractedItem(cat, disp, namespace,
                                                 current_effect, var_name))
                matched = True
                break
        if matched:
            continue

        # ── 单捕获组: group/param ──
        for cat, pattern in ANNOTATION_1G:
            m = pattern.match(line)
            if m:
                disp = m.group(1).strip()
                if not should_skip(disp):
                    results.append(ExtractedItem(cat, disp, namespace,
                                                 current_effect, ""))
                break

    return results


# ═══════════════════════════════════════════════════════════════
# 目录扫描
# ═══════════════════════════════════════════════════════════════

def parse_directory(
    script_dir: str,
    target_ns: Optional[str] = None,
    extensions: Tuple[str, ...] = ('.anm2', '.obj2', '.tra2'),
) -> Dict[str, List[ExtractedItem]]:
    """扫描目录，按命名空间分组"""
    ns_items: Dict[str, List[ExtractedItem]] = defaultdict(list)

    if not os.path.isdir(script_dir):
        print(f"[错误] 目录不存在: {script_dir}", file=sys.stderr)
        return ns_items

    for root, dirs, files in os.walk(script_dir):
        for fname in sorted(files):
            if not fname.lower().endswith(extensions):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, script_dir)
            print(f"  解析: {rel}")

            items = parse_script_file(fpath)
            for item in items:
                if target_ns and item.namespace != target_ns:
                    continue
                ns_items[item.namespace].append(item)

    return ns_items


# ═══════════════════════════════════════════════════════════════
# .aul2 生成器
# ═══════════════════════════════════════════════════════════════

def generate_aul2(
    namespace: str,
    items: List[ExtractedItem],
    output_dir: str,
    target_lang: str = "zh",
    overwrite: bool = False,
) -> Optional[str]:
    """生成 zh.XXX.aul2 翻译模板。已存在则跳过（除非 overwrite=True）。"""
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{target_lang}.{namespace}.aul2"
    filepath = os.path.join(output_dir, filename)

    if os.path.exists(filepath) and not overwrite:
        print(f"  [跳过] {filepath} 已存在 (使用 --force 强制覆盖)")
        return None

    # 按效果分组
    effect_items: Dict[str, List[ExtractedItem]] = defaultdict(list)
    label_items: List[ExtractedItem] = []
    effect_order: List[str] = []

    for item in items:
        if item.category == "effect_name":
            if item.effect_name not in effect_order:
                effect_order.append(item.effect_name)
            effect_items[item.effect_name].append(item)
        elif item.category == "label":
            label_items.append(item)
        else:
            effect_items[item.effect_name].append(item)
            if item.effect_name not in effect_order:
                effect_order.append(item.effect_name)

    lines: List[str] = []
    def w(s: str = ""):
        lines.append(s)

    w(";===============================================================")
    w(f"; Target: {namespace}")
    w(f"; Language: {target_lang}")
    w(f"; Generated by aviutl2_l10n.py v2.0")
    w(";")
    w("; 待翻译: 请将右侧空白处填写为对应译文")
    w(";===============================================================")
    w()

    # 效果 sections
    for ename in effect_order:
        eff_list = effect_items[ename]
        w(f"[{ename}]")
        w(f"{ename}=")

        seen = set()
        for item in eff_list:
            if item.category == "effect_name":
                continue
            ja = item.japanese.strip()
            if ja and ja not in seen:
                seen.add(ja)
                w(f"{ja}=")
        w()

    # [Effect] section: labels
    if label_items:
        w("; Labels (菜单分类路径)")
        w("[Effect]")
        w()
        seen_labels = set()
        for item in label_items:
            ja = item.japanese.strip()
            if ja and ja not in seen_labels:
                seen_labels.add(ja)
                w(f"{ja}=")
        w()

    content = '\n'.join(lines) + '\n'
    with open(filepath, 'w', encoding='utf-8-sig') as f:
        f.write(content)

    return filepath


# ═══════════════════════════════════════════════════════════════
# AI 翻译 (DeepSeek API)
# ═══════════════════════════════════════════════════════════════

def _build_translation_system_prompt(target_lang: str) -> str:
    """构建翻译 system prompt（源语言自动检测）"""
    return (
        f"You are a professional translator for UI localization. "
        f"Translate the following text to {target_lang}. "
        f"Rules:\n"
        f"- Output ONLY the translated text, nothing else. No explanations, no notes.\n"
        f"- Keep it concise — these are UI labels, menu items, parameter names.\n"
        f"- Preserve any technical formatting like {{}} placeholders or \\n escapes.\n"
        f"- If the text is already in {target_lang}, return it as-is.\n"
        f"- Never output 'Translation:' prefix or quotation marks around the result."
    )

def translate_text(
    text: str,
    client: OpenAI,
    model: str = DEEPSEEK_DEFAULT_MODEL,
    source_lang: str = "Japanese",
    target_lang: str = "Chinese",
) -> Optional[str]:
    """调用 DeepSeek API 翻译单条文本。

    Returns:
        翻译后的文本，失败返回 None
    """
    system_prompt = _build_translation_system_prompt(target_lang)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
            max_tokens=500,
            stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"  [API Error] {e}", file=sys.stderr)
        return None


def _batch_translate(
    texts: List[str],
    client: OpenAI,
    model: str = DEEPSEEK_DEFAULT_MODEL,
    source_lang: str = "Japanese",
    target_lang: str = "Chinese",
) -> List[Optional[str]]:
    """批量翻译多条文本（在一次 API 调用中完成）。

    使用 JSON 数组格式让模型一次性返回所有译文，速度快很多。
    """
    if not texts:
        return []

    system_prompt = (
        _build_translation_system_prompt(target_lang)
        + "\nI will give you a JSON array of strings. Translate each one and return "
          "EXACTLY a JSON array of translated strings in the same order. No other output."
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=2000,
            stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = response.choices[0].message.content.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list) and len(parsed) == len(texts):
            return [s.strip() if isinstance(s, str) else None for s in parsed]
        # 回退：对不齐的话当单条处理
        print(f"  [警告] 批量翻译返回格式异常，回退逐条翻译", file=sys.stderr)
        return [translate_text(t, client, model, source_lang, target_lang) for t in texts]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  [警告] 批量翻译解析失败 ({e})，回退逐条翻译", file=sys.stderr)
        return [translate_text(t, client, model, source_lang, target_lang) for t in texts]
    except Exception as e:
        print(f"  [API Error] {e}", file=sys.stderr)
        return [None] * len(texts)


def translate_aul2_file(
    filepath: str,
    client: OpenAI,
    model: str = DEEPSEEK_DEFAULT_MODEL,
    source_lang: str = "Japanese",
    target_lang: str = "Chinese",
    dry_run: bool = False,
    batch_size: int = 15,
) -> Tuple[int, int]:
    """翻译单个 .aul2 文件中所有未翻译条目。

    .aul2 格式: japanese=  (右侧为空 = 待翻译)
    翻译后写入:  japanese=中文译文

    Args:
        filepath: .aul2 文件路径
        api_key: DeepSeek API key
        model: 模型名
        source_lang: 源语言
        target_lang: 目标语言
        dry_run: True 时只打印不写入
        batch_size: 每批翻译条数

    Returns:
        (成功翻译数, 总数)
    """
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    lines = content.split("\n")
    # 收集所有待翻译条目: (行号, 日文)
    untranslated: List[Tuple[int, str]] = []

    for i, line in enumerate(lines):
        stripped = line.rstrip()
        # 跳过注释行、section 头、空行
        if stripped.startswith(";") or stripped.startswith("[") or not stripped.strip():
            continue
        if "=" not in stripped:
            continue
        ja, _, zh = stripped.partition("=")
        ja = ja.strip()
        zh = zh.strip()
        if not zh and ja:
            untranslated.append((i, ja))

    total = len(untranslated)
    if total == 0:
        print(f"  [跳过] {os.path.basename(filepath)} — 无需翻译")
        return 0, 0

    print(f"  {os.path.basename(filepath)}: {total} 条待翻译, 分批处理中...", end="", flush=True)

    translated = 0
    for chunk_start in range(0, total, batch_size):
        chunk_end = min(chunk_start + batch_size, total)
        chunk = untranslated[chunk_start:chunk_end]
        texts = [t[1] for t in chunk]

        if len(chunk) == 1:
            results = [translate_text(texts[0], client, model, source_lang, target_lang)]
        else:
            results = _batch_translate(texts, client, model, source_lang, target_lang)

        for (line_no, ja), result in zip(chunk, results):
            if result:
                lines[line_no] = f"{ja}={result}"
                translated += 1
            else:
                print(f"\n    [失败] {ja}", file=sys.stderr)

        if chunk_end < total:
            time.sleep(TRANSLATE_BATCH_DELAY)

    if not dry_run and translated > 0:
        with open(filepath, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines))

    status = " [预览]" if dry_run else " ✓"
    print(f" {translated}/{total} 条{status}")
    return translated, total


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def cmd_parse(args):
    script_dir = args.script_dir
    ns_filter = args.namespace or None
    print(f"扫描目录: {script_dir}")
    if ns_filter:
        print(f"命名空间过滤: {ns_filter}")
    print()

    ns_items = parse_directory(script_dir, target_ns=ns_filter)

    total = 0
    for ns, items in sorted(ns_items.items()):
        print(f"\n{'=' * 60}")
        print(f"命名空间: {ns}  ({len(items)} 条)")
        print(f"{'=' * 60}")
        cat_count = defaultdict(int)
        for item in items:
            cat_count[item.category] += 1
        for cat, cnt in sorted(cat_count.items()):
            print(f"  {cat}: {cnt}")
        total += len(items)

    print(f"\n总计: {total} 条可翻译文本, {len(ns_items)} 个命名空间")


def cmd_generate(args):
    script_dir = args.script_dir
    output_dir = args.output_dir
    ns_filter = args.namespace or None
    lang = args.lang or "zh"

    print(f"扫描目录: {script_dir}")
    print(f"输出目录: {output_dir}")
    if ns_filter:
        print(f"命名空间过滤: {ns_filter}")
    print()

    ns_items = parse_directory(script_dir, target_ns=ns_filter)

    generated = 0
    skipped = 0
    for ns, items in sorted(ns_items.items()):
        filepath = generate_aul2(ns, items, output_dir, target_lang=lang,
                                 overwrite=args.force)
        if filepath:
            print(f"  生成: {filepath}  ({len(items)} 条)")
            generated += 1
        else:
            skipped += 1

    print(f"\n完成! 生成 {generated} 个文件, 跳过 {skipped} 个已有文件。")
    if skipped:
        print("  提示: 使用 --force 可覆盖已有文件。")


# ═══════════════════════════════════════════════════════════════
# API Key 本地存储 (脚本同目录下的 .deepseek_key)
# ═══════════════════════════════════════════════════════════════

def _get_key_file() -> str:
    """返回 key 文件路径（与脚本同目录）"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, ".deepseek_key")


def _load_api_key() -> Optional[str]:
    """从本地文件加载 API key。不存在返回 None。"""
    key_file = _get_key_file()
    if not os.path.isfile(key_file):
        return None
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            key = f.read().strip()
        return key or None
    except (IOError, UnicodeDecodeError):
        return None


def _save_api_key(api_key: str):
    """保存 API key 到本地文件"""
    key_file = _get_key_file()
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(api_key.strip())
    # 设置文件权限为仅当前用户可读 (非 Windows 平台)
    if os.name != "nt":
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    print(f"  API key 已保存到: {key_file}")


def cmd_translate(args):
    """AI 翻译子命令"""
    output_dir = args.output_dir

    # ── API key 获取优先级 ──
    api_key: Optional[str] = None

    if args.api_key:
        api_key = args.api_key.strip()
        src = "命令行参数"
    else:
        api_key = _load_api_key()
        if api_key:
            src = "本地文件 (.deepseek_key)"
        else:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            if api_key:
                api_key = api_key.strip()
                src = "环境变量"
            else:
                # 交互输入 (不在管道模式下)
                if sys.stdin.isatty():
                    src = "交互输入"
                    print("首次使用需要输入 DeepSeek API key")
                    print("（获取地址: https://platform.deepseek.com/api_keys）")
                    try:
                        api_key = input("API key: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print("\n[取消]", file=sys.stderr)
                        sys.exit(1)
                if not api_key:
                    print("[错误] 未提供 API key，以下方式任选:", file=sys.stderr)
                    print("  1. 交互输入 (直接运行不加 -k)", file=sys.stderr)
                    print("  2. 命令行: --api-key sk-xxx", file=sys.stderr)
                    print("  3. 环境变量: set DEEPSEEK_API_KEY=sk-xxx", file=sys.stderr)
                    sys.exit(1)

    if not api_key:
        print("[错误] API key 无效", file=sys.stderr)
        sys.exit(1)

    # 首次交互输入或 --save-key 时保存到本地文件
    if src == "交互输入" or args.save_key:
        _save_api_key(api_key)
    elif src == "本地文件 (.deepseek_key)":
        print(f"  [OK] 已从本地文件加载 API key")
    else:
        print(f"  [OK] API key 来源: {src}")

    # 收集所有待翻译的 .aul2 文件
    aul2_files: List[str] = []
    if not os.path.isdir(output_dir):
        print(f"[错误] 目录不存在: {output_dir}", file=sys.stderr)
        sys.exit(1)

    for fname in sorted(os.listdir(output_dir)):
        if fname.endswith(".aul2"):
            aul2_files.append(os.path.join(output_dir, fname))

    if not aul2_files:
        print(f"[错误] 目录中没有 .aul2 文件: {output_dir}", file=sys.stderr)
        sys.exit(1)

    ns_filter = args.namespace
    if ns_filter:
        # 过滤: 只翻译匹配命名空间的文件 (如 zh.Basic_S.aul2)
        aul2_files = [f for f in aul2_files
                      if f".{ns_filter}.aul2" in os.path.basename(f)]
        if not aul2_files:
            print(f"[错误] 没有匹配命名空间 '{ns_filter}' 的 .aul2 文件", file=sys.stderr)
            sys.exit(1)

    model = args.model or DEEPSEEK_DEFAULT_MODEL
    source_lang = args.source_lang or "Japanese"
    target_lang = args.lang or "Chinese"
    batch_size = args.batch_size or 15
    dry_run = args.dry_run

    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    print(f"AI 翻译 (DeepSeek)")
    print(f"  目录: {output_dir}")
    print(f"  模型: {model}")
    print(f"  源语言 → 目标语言: {source_lang} → {target_lang}")
    print(f"  文件数: {len(aul2_files)}")
    if dry_run:
        print(f"  [预览模式] 不写入文件")
    print()

    total_translated = 0
    total_entries = 0
    for filepath in aul2_files:
        t, n = translate_aul2_file(
            filepath, client, model=model,
            source_lang=source_lang, target_lang=target_lang,
            dry_run=dry_run, batch_size=batch_size,
        )
        total_translated += t
        total_entries += n

    print(f"\n完成! 翻译 {total_translated}/{total_entries} 条。")
    if dry_run:
        print("  提示: 去掉 --dry-run 即可写入文件。")


def main():
    parser = argparse.ArgumentParser(
        description="AviUtl2 脚本翻译解析器 & .aul2 生成器 v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python aviutl2_l10n.py parse Script/
  python aviutl2_l10n.py generate Script/ Language/
  python aviutl2_l10n.py generate Script/ Language/ --namespace Basic_S --force
        """,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    p_parse = sub.add_parser("parse", help="解析并打印提取结果")
    p_parse.add_argument("script_dir", help="脚本目录路径")
    p_parse.add_argument("--namespace", "-n", help="只处理指定命名空间")

    p_gen = sub.add_parser("generate", help="生成 .aul2 翻译模板")
    p_gen.add_argument("script_dir", help="脚本目录路径")
    p_gen.add_argument("output_dir", help="输出目录 (Language/)")
    p_gen.add_argument("--namespace", "-n", help="只处理指定命名空间")
    p_gen.add_argument("--lang", "-l", default="zh", help="目标语言前缀 (默认: zh)")
    p_gen.add_argument("--force", "-f", action="store_true", help="强制覆盖已有文件")

    p_trans = sub.add_parser("translate", help="AI 翻译 .aul2 文件中未翻译的条目 (DeepSeek)")
    p_trans.add_argument("output_dir", help=".aul2 文件所在目录 (Language/)")
    p_trans.add_argument("--api-key", "-k", help="DeepSeek API key (也可用环境变量 DEEPSEEK_API_KEY)")
    p_trans.add_argument("--model", "-m", default=DEEPSEEK_DEFAULT_MODEL,
                         help=f"模型名 (默认: {DEEPSEEK_DEFAULT_MODEL})")
    p_trans.add_argument("--source-lang", "-s", default="Japanese", help="源语言 (默认: Japanese)")
    p_trans.add_argument("--lang", "-l", default="Chinese", help="目标语言 (默认: Chinese)")
    p_trans.add_argument("--namespace", "-n", help="只处理指定命名空间 (如 Basic_S)")
    p_trans.add_argument("--batch-size", "-b", type=int, default=15,
                         help="每批翻译条数 (默认: 15)")
    p_trans.add_argument("--dry-run", "-d", action="store_true",
                        help="预览模式，只显示结果不写入文件")
    p_trans.add_argument("--save-key", "-S", action="store_true",
                        help="将 API key 保存到脚本同目录 .deepseek_key 文件")

    args = parser.parse_args()
    if args.command == "parse":
        cmd_parse(args)
    elif args.command == "generate":
        cmd_generate(args)
    elif args.command == "translate":
        cmd_translate(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
