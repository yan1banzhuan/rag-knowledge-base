# RAG 项目从零上传 GitHub 完整指南

本文档适用于当前项目：

- 本地目录：`D:\RAG_Project-main`
- GitHub 用户：`yan1banzhuan`
- 新仓库名称：`rag-knowledge-base`
- 仓库主页：<https://github.com/yan1banzhuan/rag-knowledge-base>
- Git 远程地址：`https://github.com/yan1banzhuan/rag-knowledge-base.git`

本文分别说明：

1. 从完全没有 Git 仓库开始上传。
2. 当前项目已经初始化 Git 时如何安全推送。
3. 后续修改代码后如何更新 GitHub。
4. 密钥、大文件、冲突和认证错误如何处理。

---

## 1. 上传前必须理解的三个概念

### 1.1 本地仓库

本地仓库就是电脑中的项目目录。执行 `git init` 后，目录中会出现隐藏的 `.git` 文件夹。

### 1.2 远程仓库

远程仓库是 GitHub 上的项目。当前项目使用：

```text
https://github.com/yan1banzhuan/rag-knowledge-base
```

### 1.3 提交和推送

```text
git add      选择准备提交的文件
git commit   在本地保存一个版本
git push     把本地版本上传到 GitHub
```

`commit` 不等于上传。只有成功执行 `git push`，GitHub 页面上才会出现新版本。

---

## 2. 本项目哪些内容应该上传

建议上传：

- `app/` 后端源码
- `frontend/src/` 前端源码
- `frontend/package.json`
- `frontend/package-lock.json`
- `main.py`
- `requirements.txt`
- `requirements-voice.txt`
- `.env.example`
- `.gitignore`
- `README.md`
- 项目说明和学习文档
- 测试与评估脚本

不应该上传：

- `.env`：包含数据库密码和 API Key
- `.conda/`：本地 Python 虚拟环境
- `frontend/node_modules/`：前端依赖
- `frontend/dist/`：前端构建产物
- `data/uploads/`：用户上传的原始文件
- `data/chroma_db/`：本地向量数据库
- `logs/`：运行日志
- `__pycache__/`、`*.pyc`：Python 缓存
- Hugging Face 模型缓存
- MySQL 数据目录

这些内容已经由项目根目录的 `.gitignore` 排除。

---

## 3. 安全要求：严禁上传真实密钥

当前项目的真实配置保存在：

```text
D:\RAG_Project-main\.env
```

必须确认 `.env` 被忽略：

```powershell
cd D:\RAG_Project-main
git check-ignore -v .env
```

正常情况下会显示 `.gitignore` 中命中 `.env` 的规则。

再检查 `.env` 是否已经被 Git 跟踪：

```powershell
git ls-files .env
```

正确结果应当没有任何输出。

还可以检查准备提交的内容中是否意外包含密钥文件：

```powershell
git status --short
git diff --cached --name-only
```

注意：DeepSeek 和硅基流动密钥曾经在聊天内容中明文出现。正式公开仓库前，建议在两个服务商控制台重新生成密钥，然后只把新密钥写入本地 `.env`，不要写入 README、代码、提交说明或截图。

`.env.example` 只能保留占位符，例如：

```env
OPENAI_API_KEY=sk-xxxx
DEEPSEEK_API_KEY=sk-xxxx
MYSQL_PASSWORD=your_mysql_password
```

---

## 4. 安装和确认 Git

查看 Git 是否已经安装：

```powershell
git --version
```

配置提交者姓名和邮箱：

```powershell
git config --global user.name "yan1banzhuan"
git config --global user.email "你的GitHub邮箱"
```

检查配置：

```powershell
git config --global user.name
git config --global user.email
```

邮箱可以使用 GitHub 的隐私邮箱，格式通常类似：

```text
数字+用户名@users.noreply.github.com
```

---

## 5. 在 GitHub 创建新仓库

如果仓库尚未创建：

1. 登录 <https://github.com>。
2. 点击右上角 `+`。
3. 点击 `New repository`。
4. Repository name 填写 `rag-knowledge-base`。
5. Description 可以填写：

   ```text
   FastAPI + Vue + MySQL + ChromaDB 的 RAG 知识库问答系统
   ```

6. 根据需要选择 `Public` 或 `Private`。
7. 如果本地已经有 README，不要勾选初始化 README、`.gitignore` 和 License。
8. 点击 `Create repository`。

当前目标仓库已经确定为：

```text
https://github.com/yan1banzhuan/rag-knowledge-base
```

---

## 6. 从完全没有 Git 仓库开始上传

以下步骤适用于项目目录中还没有 `.git` 的情况。

### 6.1 进入项目目录

```powershell
cd D:\RAG_Project-main
```

确认位置：

```powershell
Get-Location
```

### 6.2 初始化本地仓库

```powershell
git init
```

把默认分支统一为 `main`：

```powershell
git branch -M main
```

### 6.3 检查忽略规则

```powershell
git check-ignore -v .env
git status --short
```

必须确认以下目录没有出现在待提交列表中：

```text
.conda/
frontend/node_modules/
frontend/dist/
data/uploads/
data/chroma_db/
logs/
```

### 6.4 将文件加入暂存区

```powershell
git add .
```

这一步还没有提交，也没有上传。

### 6.5 提交前检查

查看准备提交的文件：

```powershell
git status
git diff --cached --stat
git diff --cached --name-only
```

重点确认：

- 没有 `.env`
- 没有 API Key
- 没有数据库数据
- 没有上传文件
- 没有虚拟环境和 `node_modules`
- 没有不属于项目的个人文件

如果误加入了某个文件，可以取消暂存：

```powershell
git restore --staged 文件路径
```

例如：

```powershell
git restore --staged .env
```

### 6.6 创建首次提交

```powershell
git commit -m "Initial commit: RAG knowledge base system"
```

### 6.7 添加新远程仓库

```powershell
git remote add origin https://github.com/yan1banzhuan/rag-knowledge-base.git
```

检查远程地址：

```powershell
git remote -v
```

正确结果应该只指向：

```text
https://github.com/yan1banzhuan/rag-knowledge-base.git
```

### 6.8 首次推送

```powershell
git push -u origin main
```

`-u` 会建立本地 `main` 与远程 `origin/main` 的跟踪关系。以后可以直接运行 `git push`。

---

## 7. 当前项目已经初始化 Git 时如何上传

当前项目已经存在 Git 历史，因此不需要再次执行 `git init`。

先进入项目：

```powershell
cd D:\RAG_Project-main
```

### 7.1 检查当前分支

```powershell
git branch --show-current
git status -sb
```

如果当前是 `master`，建议统一改成 `main`：

```powershell
git branch -M main
```

### 7.2 确认只保留新仓库地址

```powershell
git remote -v
```

如果 `origin` 地址不正确，执行：

```powershell
git remote set-url origin https://github.com/yan1banzhuan/rag-knowledge-base.git
```

如果存在旧远程，例如 `previous-origin`，执行：

```powershell
git remote remove previous-origin
```

再次检查：

```powershell
git remote -v
```

### 7.3 查看本地修改

```powershell
git status --short
git diff --stat
git diff
```

不要在没有检查的情况下直接提交大量文件。

### 7.4 暂存并检查

```powershell
git add .
git status
git diff --cached --stat
```

### 7.5 提交当前修改

```powershell
git commit -m "Configure DeepSeek, SiliconFlow embedding and MySQL deployment"
```

提交说明应描述这次修改内容，不要写 API Key、密码或个人隐私。

### 7.6 推送到新仓库

如果还没有远程跟踪关系：

```powershell
git push -u origin main
```

如果已经建立跟踪关系：

```powershell
git push
```

---

## 8. GitHub 登录认证

GitHub 已不支持使用账号密码直接执行 Git 推送。

Windows 上通常会弹出浏览器登录或 Git Credential Manager 登录窗口，按提示授权即可。

如果使用 Personal Access Token：

1. 打开 GitHub 设置。
2. 进入 `Developer settings`。
3. 创建 Fine-grained token 或 Classic token。
4. 至少授予目标仓库的 Contents 读写权限。
5. 推送时把 Token 当作密码使用。

不要把 Token 写入：

- Git 远程 URL
- `.env.example`
- README
- PowerShell 脚本
- Git 提交记录

推荐让 Git Credential Manager 保存凭据。

---

## 9. 后续修改代码后的标准更新流程

每次修改完成后执行：

```powershell
cd D:\RAG_Project-main
git status
git diff
git add .
git diff --cached --stat
git commit -m "简洁说明本次修改"
git push
```

常见提交说明示例：

```text
Fix document upload validation
Add DeepSeek as default chat provider
Improve hybrid retrieval and reranking
Update deployment documentation
Add RAG evaluation scripts
```

建议一个提交只完成一类相关修改，避免一次提交同时混入大量无关内容。

---

## 10. 在另一台电脑下载项目

克隆仓库：

```powershell
git clone https://github.com/yan1banzhuan/rag-knowledge-base.git
cd rag-knowledge-base
```

复制环境变量示例：

```powershell
Copy-Item .env.example .env
```

然后在本地 `.env` 中填写：

- MySQL 密码
- DeepSeek API Key
- 硅基流动 API Key
- 本地数据和模型缓存路径

安装 Python 和前端依赖：

```powershell
python -m pip install -r requirements.txt
cd frontend
npm install
```

Git 仓库不会包含：虚拟环境、MySQL 数据、ChromaDB 数据、模型缓存和真实密钥。这些内容必须在新电脑上重新准备。

---

## 11. GitHub 文件大小限制

GitHub 普通 Git 单文件限制为 100 MB。大模型、数据库和依赖目录不适合直接上传。

本项目不应提交：

- 2 GB 以上的 Reranker 模型
- Conda 环境
- `node_modules`
- MySQL 数据目录
- ChromaDB 数据目录
- 大型测试文档

如果确实需要版本管理大型文件，可以使用 Git LFS，但模型缓存和数据库通常更适合对象存储、Release、网盘或部署时下载。

安装 Git LFS：

```powershell
git lfs install
git lfs track "*.onnx"
git lfs track "*.bin"
```

执行后需要提交 `.gitattributes`。但当前项目不建议把 Reranker 模型直接提交到 GitHub。

---

## 12. 常见错误处理

### 12.1 `remote origin already exists`

说明已经存在 `origin`，不要重复添加。改为：

```powershell
git remote set-url origin https://github.com/yan1banzhuan/rag-knowledge-base.git
```

### 12.2 `src refspec main does not match any`

通常表示还没有提交，或者当前分支不是 `main`：

```powershell
git status
git add .
git commit -m "Initial commit"
git branch -M main
git push -u origin main
```

### 12.3 `rejected non-fast-forward`

远程仓库已经有本地没有的提交。先拉取并变基：

```powershell
git pull --rebase origin main
git push
```

如果出现冲突，先解决冲突，不要直接使用 `git push --force`。

### 12.4 `403` 或没有权限

检查：

- 是否登录了正确的 GitHub 账号
- 是否对仓库有写权限
- Token 是否具有 Contents 写权限
- Windows 凭据管理器是否保存了错误账号

### 12.5 `Authentication failed`

GitHub 不接受账号密码推送。使用浏览器授权、Git Credential Manager 或 Personal Access Token。

### 12.6 文件超过 100 MB

先从暂存区移除：

```powershell
git restore --staged 大文件路径
```

然后把对应路径加入 `.gitignore`。

如果大文件已经进入提交历史，仅删除工作区文件可能仍无法推送，需要用 `git filter-repo` 清理历史。

### 12.7 LF/CRLF 警告

Windows 可能显示：

```text
LF will be replaced by CRLF
```

这通常是换行符提示，不是推送失败。团队项目建议后续增加 `.gitattributes` 统一换行策略。

---

## 13. 密钥误提交后的处理

如果密钥只被 `git add`，尚未提交：

```powershell
git restore --staged .env
```

如果已经提交但还没有推送：

```powershell
git rm --cached .env
git commit --amend --no-edit
```

如果已经推送到 GitHub：

1. 立即在服务商后台撤销并重新生成密钥。
2. 从代码中移除密钥。
3. 清理 Git 历史。
4. 强制推送清理后的历史前，通知其他协作者。

仅删除 GitHub 页面上的文件并不能保证旧密钥消失，因为旧提交仍然可以被查看。

---

## 14. 推荐的分支使用方式

个人开发可以使用：

```text
main：稳定版本
dev：日常开发
feature/功能名：具体功能开发
```

创建开发分支：

```powershell
git switch -c dev
git push -u origin dev
```

开发新功能：

```powershell
git switch dev
git switch -c feature/docker-deployment
```

功能完成后通过 GitHub Pull Request 合并到 `main`。

---

## 15. 当前项目的安全上传检查清单

推送前逐项确认：

- [ ] GitHub 目标是 `yan1banzhuan/rag-knowledge-base`
- [ ] `git remote -v` 中没有旧仓库地址
- [ ] `.env` 没有被 Git 跟踪
- [ ] DeepSeek 和硅基流动密钥没有出现在代码、文档和截图中
- [ ] `.conda/` 没有进入提交
- [ ] `frontend/node_modules/` 没有进入提交
- [ ] `data/uploads/` 没有进入提交
- [ ] `data/chroma_db/` 没有进入提交
- [ ] `logs/` 没有进入提交
- [ ] 没有超过 100 MB 的文件
- [ ] `git diff --cached` 已人工检查
- [ ] README 中的仓库地址正确
- [ ] 提交说明没有密码和密钥
- [ ] 推送后在 GitHub 页面再次检查文件列表

---

## 16. 当前项目推荐执行的上传命令

当前项目已经初始化过 Git，并已经配置新 `origin`。完成代码检查后，可以执行：

```powershell
cd D:\RAG_Project-main

git status
git remote -v
git check-ignore -v .env
git diff

git branch -M main
git add .
git diff --cached --stat
git diff --cached --name-only

git commit -m "Update RAG system configuration and documentation"
git push -u origin main
```

执行 `git add .` 后必须先检查暂存区，再执行 `commit` 和 `push`。

仓库最终地址：

```text
https://github.com/yan1banzhuan/rag-knowledge-base
```
