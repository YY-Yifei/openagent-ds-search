# 发版手册

给自己看的清单，照顺序走一遍就不会漏。

## 0. 先把「要发的东西」准备好

```bash
python -m pytest -o addopts="" -q          # 必须全绿（当前 128 passed）
```

要改版本号的话，三个地方必须同时改（现在都是 `0.1.0`）：

| 文件 | 位置 |
|---|---|
| `pyproject.toml` | `[project] version` |
| `src/openagent_ds_search/__init__.py` | `__version__` |
| `CHANGELOG.md` | 加一节 `## [x.y.z] — YYYY-MM-DD` |

## 1. 本地构建 + 自检

```bash
rmdir /s /q dist build            # Windows；Linux/macOS 用 rm -rf
python -m build                   # 产出 dist/*.tar.gz 与 dist/*.whl
python -m twine check dist/*      # 两个都要 PASSED
```

想看 wheel 里到底装了什么：

```bash
python -c "import zipfile,glob;z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]);print('\n'.join(z.namelist()))"
```

## 2. 推 GitHub

**第一步永远是先去 https://github.com/new 建一个「空」仓库**（名字 `openagent-ds-search`，
不要勾 README / .gitignore / license —— 本地已经有了）。没有 token 就没法用 API 建仓，
这一步只能手工点。

建好之后二选一：

### 路线 A：HTTPS（有 PAT 时最省事）

```bash
git remote set-url origin https://github.com/YY-Yifei/openagent-ds-search.git
git push -u origin main
git push origin v0.1.0
```

PAT 在 https://github.com/settings/tokens 生成（勾 `repo`）。凭据会被 Windows
凭据管理器记住，不用每次输。

### 路线 B：SSH（本机实测 443 连不上时走这条）

这台机器上 `git push` 走 HTTPS 会卡在
`Failed to connect to github.com port 443`，而 `ssh -T git@github.com` 是**通的**
（返回 `Permission denied (publickey)` —— 说明能连上，只是公钥还没登记）。
所以：

1. 把本机公钥贴进 https://github.com/settings/keys → New SSH key：

   ```
   ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMbgdFXybdo8hBnPh768cmGixPNRfkxX/DFXyprmGRmF 274529723+YY-Yifei@users.noreply.github.com
   ```

2. 验证：`ssh -T git@github.com` 应回 `Hi YY-Yifei! You've successfully authenticated...`

3. 推：

   ```bash
   git remote set-url origin git@github.com:YY-Yifei/openagent-ds-search.git
   git push -u origin main
   git push origin v0.1.0
   ```

> 仓库里已经配好 `origin`（HTTPS 地址）与 tag `v0.1.0`，只差一次 `push`。

之后每次发版：

```bash
git add -A && git commit -m "release: v0.1.1"
git tag -a v0.1.1 -m "v0.1.1"
git push && git push --tags
```

## 3. 发 PyPI

**Windows 控制台上传前必须先切 UTF-8**，否则 twine 的 rich 进度条会在
`0%` 处 `UnicodeEncodeError: 'gbk' codec can't encode character '\u2022'` 崩掉
（上传没成，PyPI 上什么都不会留下）：

```bash
chcp 65001
set "PYTHONUTF8=1"
```

（`set "VAR=1"` 的引号不能省 —— `set VAR=1 & ...` 会把尾随空格一起写进变量值，
Python 会直接 `Fatal Python error: preconfig_init_utf8_mode`。）

凭据二选一：

- **token 写进 `~/.pypirc`**（本机已有）：
  ```ini
  [pypi]
  username = __token__
  password = pypi-xxxxxxxx...
  ```
- **临时用环境变量**（CI 里更合适）：
  ```bash
  set TWINE_USERNAME=__token__
  set TWINE_PASSWORD=pypi-xxxxxxxx...
  ```

上传（先用 TestPyPI 试一次再上正式的最稳）：

```bash
# 可选：先上测试站（需要一个 TestPyPI 的 token，写进 .pypirc 的 [testpypi] 段）
python -m twine upload --repository testpypi dist/*

# 正式
python -m twine upload dist/*
```

**PyPI 上同一个 `版本号` 只能传一次，传错了只能提版本 —— 所以先 twine check，再上传。**

## 4. 发完立刻验证（换一台干净环境最可信）

```bash
python -m venv %TEMP%\verify && %TEMP%\verify\Scripts\activate
pip install openagent-ds-search
openagent-ds-search --help
set DEEPSEEK_API_KEY=sk-...
openagent-ds-search --check
```

⚠️ 本机 pip 全局源是**阿里云镜像**（`pip config list` 可见），新包上传后镜像同步有延迟，
刚发完可能报 `Could not find a version that satisfies the requirement`。验证时加
`--index-url https://pypi.org/simple` 才是查 PyPI 本体，别被镜像骗了。

PyPI 页面：https://pypi.org/project/openagent-ds-search/

## 5. 想让别人「装好配置环境变量就能用」，页面必须说清三件事

1. `pip install openagent-ds-search`
2. `DEEPSEEK_API_KEY` 怎么设（Windows / Linux / macOS 各一行）
3. `openagent-ds-search --check` 自检 —— 这一步能把「key 没生效 / 模型名被路由 /
   上下文配置过乐观」全查出来，比让用户拿真问题试错便宜得多。

以上三件事 README 的「安装」「运行」「体检」三节已经覆盖。
