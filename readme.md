1. Pip install all the required packages

- pip install -r requirements.txt

2. Install the Playwright extensions

- python -m playwright install

3. Update the inputs.yaml file with your preferred criteria
4. Run the script in a new terminal

- python .\run_linkedin.py

5.

Note ther is some cokkie issue in indded and naukri

How its work
1st input.yaml file
-> update Your preffrence
2nd run run_linkedin.py
wait for result in result folder done

帖子采集：

```bash
python run_posts.py --industry manufacturing
python run_posts.py --industry education
```

首次使用先执行 `python run_posts.py --login`。这会打开电脑上安装的正式版Google Chrome，登录窗口不连接Playwright。需要自行登录LinkedIn、完成验证，进入主页后手动关闭这次打开的Chrome窗口，再执行采集命令。不需要把账号或密码写入代码或发送给助手。

搜索词在`inputs_posts.yaml`，时间为Past month，类型为Posts。登录和采集共用项目目录下的`linkedin_profile_chrome/`，后续复用本地登录状态；登录失效时重新运行`--login`。采集也使用已安装的正式Chrome，不再使用测试版Chromium。
结果保存到新的`results/<MFG或EDU>_<时间戳>/`目录，每次运行单独保存：

- `MFG_posts_import.csv` / `EDU_posts_import.csv`：作业指定的19列帖子表。
- `*_comments_import.csv`：指定的10列评论表，只读取前30条保留帖子的至多5条可见顶层评论。
- `*_comment_coverage.csv`：每条保留帖子的评论检查情况，未检查和未加载均有单独状态。
- `*_collection_summary.json`：实际查询、可确认的日期筛选、候选数量、缺失字段、去重和停止原因。
- `*_rejected_candidates.json`：未保留候选的原因。
- `*_posts_candidates.csv`：便于排查的候选表，包含未满足导入条件的记录；不能作为合格样本提交。

WSL 中只有 Windows Chrome 时，命令会自动交给项目的 `.venv-windows` 运行，避免 Linux 驱动直接控制 Windows 浏览器。该环境在本次工作中准备；换电脑后可在项目目录的 Windows PowerShell 中初始化：

```powershell
py -3.12 -m venv .venv-windows
.\.venv-windows\Scripts\python.exe -m pip install -r requirements_windows.txt
```

在项目目录的 PowerShell 中启动制造业帖子采集：

```powershell
.\.venv-windows\Scripts\python.exe run_posts.py --industry manufacturing
```
