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

首次（仓库还没建，去 https://github.com/new 建一个**空**仓库，别勾 README/.gitignore）：

```bash
git remote add origin https://github.com/YY-Yifei/openagent-ds-search.git
git branch -M main
git push -u origin main
git push origin v0.1.0
```

之后每次发版：

```bash
git add -A && git commit -m "release: v0.1.1"
git tag -a v0.1.1 -m "v0.1.1"
git push && git push --tags
```

## 3. 发 PyPI

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

PyPI 页面：https://pypi.org/project/openagent-ds-search/

## 5. 想让别人「装好配置环境变量就能用」，页面必须说清三件事

1. `pip install openagent-ds-search`
2. `DEEPSEEK_API_KEY` 怎么设（Windows / Linux / macOS 各一行）
3. `openagent-ds-search --check` 自检 —— 这一步能把「key 没生效 / 模型名被路由 /
   上下文配置过乐观」全查出来，比让用户拿真问题试错便宜得多。

以上三件事 README 的「安装」「运行」「体检」三节已经覆盖。
