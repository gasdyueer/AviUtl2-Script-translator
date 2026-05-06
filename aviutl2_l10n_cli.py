#!/usr/bin/env python3
"""
AviUtl2 翻译工具 — 交互式命令行
================================
用法:
  python aviutl2_l10n_cli.py                          # 默认 Script/ → Language/
  python aviutl2_l10n_cli.py -s ./Script -o ./Lang    # 自定义路径
  python aviutl2_l10n_cli.py -s ./Script -n Basic_S   # 启动即过滤

命令 (括号内为简写):
  scan     (sc)     重新扫描脚本目录
  list     (ls)     列出所有命名空间
  show <ns>         查看某命名空间的翻译条目详情
  preview  (pv) <ns> 预览生成的 .aul2 内容 (前 30 行)
  gen      (g) <ns>  生成 zh.<ns>.aul2 到输出目录
  gen      (g) <ns> sub  生成到输出目录的 sub 子目录内
  gen      (g) all       生成所有命名空间
  gen      (g) <ns> -f   强制覆盖已有文件
  translate (tra) <ns>   AI 翻译指定命名空间 (DeepSeek)
  translate (tra) all     AI 翻译所有命名空间
  translate (tra) <ns> -d  AI 翻译预览 (不写入)
  set-key  (key)    设置/更新 DeepSeek API key
  config   (cfg)    查看当前路径配置
  help     (h)      帮助
  quit / q / exit   退出
"""
import os
import sys
import argparse
import shutil
import subprocess
from collections import defaultdict
from typing import Optional

# DeepSeek API
from openai import OpenAI

# 添加自身目录到 path，确保能 import aviutl2_l10n
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aviutl2_l10n import (
    parse_directory,
    generate_aul2,
    translate_aul2_file,
    TransProgress,
    _get_key_file,
    _load_api_key,
    _save_api_key,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
)


# ═══════════════════════════════════════════════════════════════
# 终端输出辅助
# ═══════════════════════════════════════════════════════════════

class Term:
    """轻量终端颜色 (Windows 10+ / Linux / macOS)"""
    _enabled = True

    @classmethod
    def init(cls):
        # 非 TTY 时禁用颜色
        if not sys.stdout.isatty():
            cls._enabled = False
            return
        if sys.platform == 'win32':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

    RED    = '\033[91m'
    GREEN  = '\033[92m'
    YELLOW = '\033[93m'
    BLUE   = '\033[94m'
    MAGENTA= '\033[95m'
    CYAN   = '\033[96m'
    WHITE  = '\033[97m'
    BOLD   = '\033[1m'
    DIM    = '\033[2m'
    RESET  = '\033[0m'

    @classmethod
    def color(cls, text: str, color: str) -> str:
        if not cls._enabled:
            return text
        return f"{color}{text}{cls.RESET}"

    @classmethod
    def header(cls, text: str) -> str:
        return cls.color(text, cls.BOLD + cls.CYAN)

    @classmethod
    def ok(cls, text: str) -> str:
        return cls.color(text, cls.GREEN)

    @classmethod
    def warn(cls, text: str) -> str:
        return cls.color(text, cls.YELLOW)

    @classmethod
    def err(cls, text: str) -> str:
        return cls.color(text, cls.RED)

    @classmethod
    def dim(cls, text: str) -> str:
        return cls.color(text, cls.DIM)

    @classmethod
    def bold(cls, text: str) -> str:
        return cls.color(text, cls.BOLD)


# ═══════════════════════════════════════════════════════════════
# 交互式 REPL
# ═══════════════════════════════════════════════════════════════

class L10nREPL:
    def __init__(self, script_dir: str, output_dir: str,
                 namespace_filter: str = None, api_key: str = None,
                 save_key: bool = False):
        self.script_dir = os.path.abspath(script_dir)
        self.output_dir = os.path.abspath(output_dir)
        self.namespace_filter = namespace_filter

        # 缓存
        self._ns_items: dict = {}
        self._last_scan_ok = False

        # ── API key & client ──
        self._client: Optional[OpenAI] = None
        self._api_key: Optional[str] = None

        # 优先级: 参数 > 本地文件 > 环境变量
        if api_key:
            self._api_key = api_key.strip()
        else:
            self._api_key = _load_api_key()
            if not self._api_key:
                self._api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or None

        if self._api_key:
            self._client = OpenAI(api_key=self._api_key, base_url=DEEPSEEK_BASE_URL)

        # 命令行传入 key 且要求保存
        if save_key and self._api_key:
            _save_api_key(self._api_key)

    # ── 内部方法 ──

    def _ensure_scan(self) -> bool:
        """确保至少扫描过一次"""
        if self._ns_items:
            return True
        return self._do_scan()

    def _do_scan(self) -> bool:
        """执行扫描"""
        print(Term.dim(f"  正在扫描 {self.script_dir} ..."))
        try:
            self._ns_items = parse_directory(
                self.script_dir,
                target_ns=self.namespace_filter,
            )
            self._last_scan_ok = True
            ns_count = len(self._ns_items)
            total = sum(len(v) for v in self._ns_items.values())
            print(Term.ok(f"  扫描完成: {ns_count} 个命名空间, {total} 条可翻译文本"))
            return True
        except Exception as e:
            print(Term.err(f"  扫描失败: {e}"))
            self._last_scan_ok = False
            return False

    def _resolve_ns(self, arg: str) -> list:
        """解析命名空间参数: 支持通配符 * 和 all"""
        if arg in ("all", "*"):
            return sorted(self._ns_items.keys())
        return [ns for ns in self._ns_items if ns == arg]

    # ── 命令处理 ──

    def cmd_scan(self, args: str):
        """重新扫描脚本目录"""
        self._ns_items = {}
        self._do_scan()

    def cmd_list(self, args: str):
        """列出所有命名空间及统计"""
        if not self._ensure_scan():
            return
        print()
        total_all = 0
        for ns, items in sorted(self._ns_items.items()):
            effects = len(set(i.effect_name for i in items if i.category == "effect_name"))
            n = len(items)
            total_all += n
            print(f"  {Term.bold(ns)}  →  {n} 条  {effects} 个效果")
        print(f"  ────────────────")
        print(f"  合计: {total_all} 条, {len(self._ns_items)} 个命名空间")
        print()

    def cmd_show(self, args: str):
        """查看某命名空间的翻译条目详情  show <namespace>"""
        if not self._ensure_scan():
            return
        ns_list = self._resolve_ns(args.strip())
        if not ns_list:
            print(Term.warn(f"  未找到命名空间: {args.strip() or '(空)'}"))
            return

        for ns in ns_list:
            items = self._ns_items[ns]
            print(f"\n{Term.header('═' * 50)}")
            print(f"{Term.header('命名空间:')} {Term.bold(ns)}  ({len(items)} 条)")
            print(f"{Term.header('═' * 50)}")

            # 按类型统计
            cat_count = defaultdict(int)
            for item in items:
                cat_count[item.category] += 1

            cat_names = {
                "effect_name": "效果名", "track": "滑块", "select": "下拉参数",
                "select_option": "下拉选项", "check": "复选框", "color": "颜色",
                "file": "文件路径", "group": "分组名", "value": "隐藏值",
                "param": "脚本参数", "label": "菜单路径",
            }
            print(f"  类别统计:")
            for cat, cnt in sorted(cat_count.items()):
                print(f"    {cat_names.get(cat, cat):10s}  {cnt:>4d}")

            # 按效果分组显示前几条
            print(f"\n  {Term.dim('效果详情 (每个效果显示前3条参数):')}")
            eff_map = defaultdict(list)
            for item in items:
                if item.category != "effect_name":
                    eff_map[item.effect_name].append(item)

            for ename in sorted(eff_map):
                params = eff_map[ename]
                print(f"\n    {Term.bold(f'[{ename}]')}  ({len(params)} 个参数)")
                for p in params[:3]:
                    jp = p.japanese[:40] if len(p.japanese) <= 40 else p.japanese[:37] + "..."
                    print(f"      {Term.dim(p.category + ':')} {jp}")
                if len(params) > 3:
                    print(f"      {Term.dim('... 还有 ' + str(len(params) - 3) + ' 条')}")

            print()

    def cmd_preview(self, args: str):
        """预览生成的 .aul2 内容 (前 30 行)  preview <namespace>"""
        if not self._ensure_scan():
            return
        ns_list = self._resolve_ns(args.strip())
        if not ns_list:
            print(Term.warn(f"  未找到命名空间: {args.strip() or '(空)'}"))
            return

        for ns in ns_list:
            items = self._ns_items[ns]
            # 临时生成到内存（通过 generate 到临时目录再读取）
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                path = generate_aul2(ns, items, tmpdir, overwrite=True)
                if not path:
                    print(Term.warn(f"  生成失败"))
                    continue
                with open(path, 'r', encoding='utf-8-sig') as f:
                    lines = f.readlines()
                print(f"\n{Term.header('═══ 预览 ' + path + ' (前 30 行) ═══')}")
                for line in lines[:30]:
                    print(f"  {line.rstrip()}")
                if len(lines) > 30:
                    print(f"  {Term.dim(f'... 共 {len(lines)} 行')}")
            print()

    def cmd_gen(self, args: str):
        """生成翻译模板  gen <ns|all> [subdir] [-f]"""
        if not self._ensure_scan():
            return

        parts = args.strip().split()
        force = "-f" in parts or "--force" in parts
        # 筛掉 flag，剩下的第一个是 ns，第二个(可选)是子目录
        non_flag = [p for p in parts if p not in ("-f", "--force")]
        ns_arg = non_flag[0] if len(non_flag) >= 1 else ""
        subdir = non_flag[1] if len(non_flag) >= 2 else None

        ns_list = self._resolve_ns(ns_arg)
        if not ns_list:
            print(Term.warn(f"  未找到命名空间: {ns_arg or '(空)'}"))
            return

        # 解析最终输出目录
        actual_output = os.path.join(self.output_dir, subdir) if subdir else self.output_dir
        os.makedirs(actual_output, exist_ok=True)

        generated = 0
        skipped = 0
        for ns in ns_list:
            items = self._ns_items[ns]
            path = generate_aul2(ns, items, actual_output, overwrite=force)
            if path:
                rel = os.path.relpath(path, os.getcwd())
                print(f"  {Term.ok('✓')} {rel}  ({len(items)} 条)")
                generated += 1
            else:
                skipped += 1

        if subdir:
            print(Term.dim(f"  输出到: {actual_output}"))
        if generated:
            print(Term.ok(f"\n  生成 {generated} 个文件"))
        if skipped:
            print(Term.warn(f"  跳过 {skipped} 个 (使用 gen {ns_arg} -f 覆盖)"))

    def _ensure_key(self) -> bool:
        """确保有可用 API key。无 key 时交互输入。"""
        if self._client is not None:
            return True

        if sys.stdin.isatty():
            print(Term.warn("  AI 翻译需要 DeepSeek API key"))
            print(Term.dim("  获取地址: https://platform.deepseek.com/api_keys"))
            try:
                key = input("  API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n  {Term.warn('已取消')}")
                return False
            if key:
                self._api_key = key
                self._client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
                _save_api_key(key)
                return True
        print(Term.err("  未配置 API key，请使用 set-key 命令设置"))
        return False

    def cmd_translate(self, args: str):
        """AI 翻译: translate <ns|all> [-d]"""
        if not self._ensure_scan():
            return
        if not self._ensure_key():
            return

        parts = args.strip().split()
        dry_run = "-d" in parts or "--dry-run" in parts
        non_flag = [p for p in parts if p not in ("-d", "--dry-run")]
        ns_arg = non_flag[0] if non_flag else ""

        ns_list = self._resolve_ns(ns_arg)
        if not ns_list:
            print(Term.warn(f"  未找到命名空间: {ns_arg or '(空)'}"))
            return

        # 收集对应 .aul2 文件
        model = DEEPSEEK_DEFAULT_MODEL
        filepaths: list = []
        for ns in ns_list:
            fname = f"zh.{ns}.aul2"
            fpath = os.path.join(self.output_dir, fname)
            if not os.path.isfile(fpath):
                print(Term.warn(f"  跳过: {fname} 不存在 (先 gen {ns} 生成模板)"))
                continue
            filepaths.append(fpath)

        if not filepaths:
            print(Term.warn("  没有可翻译的文件"))
            return

        progress = TransProgress(len(filepaths))

        total_translated = 0
        total_entries = 0
        total_failed = 0
        for fpath in filepaths:
            t, n, f = translate_aul2_file(
                fpath, self._client, model=model,
                dry_run=dry_run,
                progress=progress,
            )
            total_translated += t
            total_entries += n
            total_failed += f

        progress.summary()
        if dry_run:
            print(Term.dim("  提示: 去掉 -d 即可写入文件"))

    def cmd_set_key(self, args: str):
        """设置/更新 API key"""
        key = args.strip()
        if key:
            self._api_key = key
            self._client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
            _save_api_key(key)
            print(Term.ok("  API key 已更新"))
        elif sys.stdin.isatty():
            print(Term.dim("  获取地址: https://platform.deepseek.com/api_keys"))
            try:
                key = input("  API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n  {Term.warn('已取消')}")
                return
            if key:
                self._api_key = key
                self._client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
                _save_api_key(key)
                print(Term.ok("  API key 已保存"))
            else:
                print(Term.warn("  未输入 key"))
        else:
            print(Term.err("  非交互模式，请用: set-key sk-xxx"))

    def cmd_config(self, args: str):
        """查看当前路径配置"""
        key_status = Term.ok("已配置") if self._client else Term.warn("未配置")
        print(f"""
  {Term.bold('脚本目录:')}   {self.script_dir}
  {Term.bold('输出目录:')}   {self.output_dir}
  {Term.bold('命名空间:')}   {self.namespace_filter or '(全部)'}
  {Term.bold('已缓存:')}     {len(self._ns_items)} 个命名空间
  {Term.bold('API key:')}    {key_status}
""")

    def cmd_help(self, args: str):
        """帮助"""
        print(f"""
{Term.header('可用命令 (括号内为简写):')}
  {Term.bold('scan')}      (sc)  重新扫描脚本目录
  {Term.bold('list')}      (ls)  列出所有命名空间及统计
  {Term.bold('show')}      <ns>  查看命名空间的翻译条目详情
  {Term.bold('preview')}   (pv)  <ns>  预览生成的 .aul2 内容
  {Term.bold('gen')}       (g)   <ns>          生成 zh.<ns>.aul2 到输出目录
  {Term.bold('gen')}       (g)   <ns> <sub>    生成到输出目录的 <sub> 子目录内
  {Term.bold('gen')}       (g)   all           生成所有命名空间
  {Term.bold('gen')}       (g)   <ns> -f       强制覆盖已有文件
  {Term.bold('translate')} (tra) <ns>          AI 翻译指定命名空间
  {Term.bold('translate')} (tra) all           AI 翻译所有命名空间
  {Term.bold('translate')} (tra) <ns> -d       AI 翻译预览 (不写入)
  {Term.bold('set-key')}   (key) 设置/更新 DeepSeek API key
  {Term.bold('config')}    (cfg) 查看当前路径配置
  {Term.bold('help')}      (h)   显示此帮助
  {Term.bold('quit')}      / q / exit  退出

{Term.dim('提示: <ns> 可用 * 或 all 代表所有命名空间')}
{Term.dim('翻译需要 DeepSeek API key，首次使用 set-key (或 key)')}
""")

    # ── 主循环 ──

    def run(self):
        print(Term.header("╔══════════════════════════════════════╗"))
        print(Term.header("║   AviUtl2 翻译工具 - 交互式命令行   ║"))
        print(Term.header("╚══════════════════════════════════════╝"))
        print(Term.dim(f"  脚本: {self.script_dir}"))
        print(Term.dim(f"  输出: {self.output_dir}"))
        print(Term.dim(f"  输入 help 查看命令, quit 退出"))
        print()

        # 自动首扫
        self._do_scan()
        if self._last_scan_ok and self._ns_items:
            print(f"  {Term.dim('输入 list 查看全貌, show <ns> 查看详情')}")
        print()

        while True:
            try:
                raw = input(f"{Term.GREEN}l10n{Term.RESET}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  bye~")
                break

            if not raw:
                continue

            # 解析命令
            parts = raw.split(maxsplit=1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""

            if cmd in ("quit", "q", "exit"):
                print("  bye~")
                break
            elif cmd in ("scan", "sc"):
                self.cmd_scan(rest)
            elif cmd in ("list", "ls"):
                self.cmd_list(rest)
            elif cmd == "show":
                self.cmd_show(rest)
            elif cmd in ("preview", "pv"):
                self.cmd_preview(rest)
            elif cmd in ("gen", "g"):
                self.cmd_gen(rest)
            elif cmd in ("translate", "tra"):
                self.cmd_translate(rest)
            elif cmd in ("set-key", "key"):
                self.cmd_set_key(rest)
            elif cmd in ("config", "cfg"):
                self.cmd_config(rest)
            elif cmd in ("help", "h", "?"):
                self.cmd_help(rest)
            else:
                print(Term.warn(f"  未知命令: {cmd}  (输入 help 查看帮助)"))


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="AviUtl2 翻译工具 — 交互式命令行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-s", "--script-dir", default="Script",
                        help="脚本目录 (默认: Script/)")
    parser.add_argument("-o", "--output-dir", default="Language",
                        help="翻译文件输出目录 (默认: Language/)")
    parser.add_argument("-n", "--namespace", default=None,
                        help="启动时过滤的命名空间")
    parser.add_argument("-k", "--api-key", default=None,
                        help="DeepSeek API key")
    parser.add_argument("-S", "--save-key", action="store_true",
                        help="将 API key 保存到本地文件")
    args = parser.parse_args()

    Term.init()
    repl = L10nREPL(
        script_dir=args.script_dir,
        output_dir=args.output_dir,
        namespace_filter=args.namespace,
        api_key=args.api_key,
        save_key=args.save_key,
    )
    repl.run()


if __name__ == "__main__":
    main()
