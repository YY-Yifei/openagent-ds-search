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

仓库已存在：<https://github.com/YY-Yifei/openagent-ds-search>（2026-11-05 建，public）。

### 本机网络实测（别再把这条通道判错）

| 通道 | 实测 |
|---|---|
| `ssh -T git@github.com`（22 端口） | ✅ `Hi YY-Yifei! You've successfully authenticated` —— 公钥**早已登记** |
| `git push` 走 SSH | ✅ 可用，`origin` 已指向 `git@github.com:YY-Yifei/openagent-ds-search.git` |
| `https://api.github.com`（443） | ✅ 通（`curl` / `gh` 都行） |
| `git push` 走 HTTPS（443） | ❌ `Failed to connect to github.com port 443` |

**结论：推代码走 SSH，建仓 / 建 Release / 改仓库设置走 API。**
这两个是不同通道 —— HTTPS 挂掉不代表没权限，SSH 通也不代表能调 API。

### 新建仓库（换项目时才需要）

有 `GITHUB_TOKEN` 就直接打 API，不用点网页：

```bash
curl -X POST -H "Authorization: Bearer %GITHUB_TOKEN%" \
     -H "Accept: application/vnd.github+json" \
     -H "Content-Type: application/json" \
     --data-binary "@repo-create.json" \
     https://api.github.com/user/repos
```

`repo-create.json` 里**必须** `"auto_init": false` —— 否则远端会多一个初始提交，
第一次 `push` 直接冲突（`fetch first`）。

没有 token 才回退到 <https://github.com/new> 手工建，名字 `openagent-ds-search`，
**不要**勾 README / .gitignore / license（本地都有了）。

### 推

```bash
git remote set-url origin git@github.com:YY-Yifei/openagent-ds-search.git
git push -u origin main
git push origin v0.1.0
```

之后每次发版：

```bash
git add -A && git commit -m "release: v0.1.1"
git tag -a v0.1.1 -m "v0.1.1"
git push && git push --tags
```

### Release 页面（可选，但推荐）

tag 推上去不等于有 Release 页面 —— 要单独建一条，把改动说明写清楚：

```bash
curl -X POST -H "Authorization: Bearer %GITHUB_TOKEN%" \
     -H "Accept: application/vnd.github+json" \
     -H "Content-Type: application/json" \
     --data-binary "@release.json" \
     https://api.github.com/repos/YY-Yifei/openagent-ds-search/releases
```

`release.json` 的 `tag_name` 用已存在的 `v0.1.0`，`body` 从 `CHANGELOG.md` 那一节抄。

### 推完确认

```bash
git ls-remote origin                  # 看 main 与 v0.1.0 两个 ref 都在
git ls-tree --name-only origin/main   # 看远端树里有 LICENSE / pyproject.toml / src
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
