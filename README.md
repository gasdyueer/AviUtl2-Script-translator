# AviUtl2 Script Translator

解析 AviUtl2 脚本注解，生成翻译模板，并使用 DeepSeek AI 完成本地化翻译。

## 部署

将以下文件放到 `C:\ProgramData\aviutl2` 目录下：

```
C:\ProgramData\aviutl2\
├── aviutl2_l10n.py
├── aviutl2_l10n_cli.py
└── l10n.bat
```

安装依赖：

```powershell
pip install -r requirements.txt
```

## 使用

双击 `l10n.bat` 启动交互式命令行。

- 默认从 `C:\ProgramData\aviutl2\Script\` 读取脚本，翻译模板输出到 `C:\ProgramData\aviutl2\Language\`
- 首次使用先输入 `set-key` 配置 DeepSeek API key（获取地址：https://platform.deepseek.com/api_keys）
- 输入 `scan` 扫描脚本目录，`list` 查看全貌，`show <ns>` 查看详情
- 输入 `gen all` 生成所有翻译模板，`translate all` 开始 AI 翻译

也可以指定自定义路径：

```powershell
python aviutl2_l10n_cli.py -s ./MyScripts -o ./MyLang
```

## 功能

- **parse** — 扫描脚本目录，提取所有可翻译文本（效果名、滑块、下拉菜单、复选框、颜色、分组等）
- **generate** — 生成 `zh.XXX.aul2` 翻译模板，按效果分组
- **translate** — 调用 DeepSeek API 批量翻译未完成条目，支持预览模式
- **交互式 CLI** — 彩色终端 REPL，方便逐步处理每个命名空间

## 支持的注解格式

解析器兼容 AviUtl2 标准格式和旧式写法，内部变量名（`PI`, `obj`, `temp` 等）和纯数字/符号文本自动跳过。

| 类型 | 格式示例 |
|------|---------|
| 效果声明 | `--information:EffectName@Namespace` |
| 滑块 | `--track@var:显示名,` |
| 下拉菜单 | `--select@var:显示名=值1,选项1=值1,...` |
| 复选框 | `--check@var:显示名,` |
| 颜色 | `--color@var:显示名,` |
| 文件路径 | `--file@var:显示名` |
| 分组 | `--group:分组名,` |
| 脚本参数 | `--param:参数名,` |

## CLI 命令

| 命令 | 说明 |
|------|------|
| `scan` | 重新扫描脚本目录 |
| `list` | 列出所有命名空间及统计 |
| `show <ns>` | 查看命名空间的翻译条目详情 |
| `preview <ns>` | 预览生成的 `.aul2` 内容（前 30 行） |
| `gen <ns>` | 生成 `zh.<ns>.aul2` 到输出目录 |
| `gen all` | 生成所有命名空间 |
| `gen <ns> -f` | 强制覆盖已有文件 |
| `translate <ns>` | AI 翻译指定命名空间 |
| `translate all` | AI 翻译所有命名空间 |
| `translate <ns> -d` | AI 翻译预览（不写入） |
| `set-key` | 设置/更新 DeepSeek API key |
| `config` | 查看当前路径配置 |
| `help` | 帮助 |
| `quit` / `q` | 退出 |

## 翻译模板格式

```ini
;===============================================================
; Target: Basic_S
; Language: zh
;===============================================================

[EffectName]
EffectName=
显示文本1=
显示文本2=
```

右侧为空表示待翻译，AI 翻译或人工填写后写入译文。

## API Key

支持三种方式提供 DeepSeek API key（优先级从高到低）：

1. 交互式 CLI 内 `set-key` 命令（推荐，自动保存到 `.deepseek_key`）
2. 环境变量 `DEEPSEEK_API_KEY`
3. 命令行参数 `-k sk-xxx`

获取地址：https://platform.deepseek.com/api_keys

## License

MIT
