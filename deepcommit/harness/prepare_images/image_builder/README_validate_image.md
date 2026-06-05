# validate_image.py 使用说明

基于 Claude Agent 的 Docker 镜像验证工具，用于检查镜像是否正确配置以运行测试。

## 功能概述

该工具执行以下检查：
1. **镜像存在性检查** - 验证 Docker 镜像是否存在
2. **运行时环境检查** - 验证容器内环境是否正常工作
3. **测试框架检查** - 验证测试框架能否正确收集测试
4. **测试文件发现** - 验证测试文件是否被正确识别

## 前置要求

- Docker 已安装并运行
- Claude CLI 已安装并配置（除非使用 `--skip-agent`）
- Python 3.10+

## 命令行参数

### 镜像指定（二选一，必填）

| 参数 | 说明 |
|------|------|
| `--work-dir PATH` | 工作目录路径，自动推断镜像名 |
| `--image NAME` | 显式指定 Docker 镜像名 |

### 语言和测试框架

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--language` | `python` | 编程语言 |
| `--test-framework` | `pytest` | 测试框架 |
| `--test-dirs` | `test/` | 测试目录（空格分隔多个） |
| `--test-file-pattern` | `test_*.py` | 测试文件匹配模式 |
| `--config PATH` | - | 从 YAML 配置文件加载设置 |

### 构建选项

| 参数 | 说明 |
|------|------|
| `--force-rebuild` | 验证前强制重新构建镜像 |
| `--dockerfile PATH` | Dockerfile 路径（`--force-rebuild` 时必需） |
| `--repo-path PATH` | 仓库路径（`--force-rebuild` 时必需） |

### Agent 选项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--model` | `haiku` | Claude 模型 (`haiku`/`sonnet`/`opus`) |
| `--timeout` | `300` | Agent 超时时间（秒） |
| `--skip-agent` | - | 跳过 Agent 验证，仅检查镜像存在性 |

### 输出选项

| 参数 | 说明 |
|------|------|
| `--output PATH` | 保存验证结果到 JSON 文件 |
| `--quiet` | 静默模式，不输出进度信息 |

## 使用示例

### 基础验证

```bash
# 使用工作目录（自动推断镜像名）
python validate_image.py --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/base

# 指定镜像和框架
python validate_image.py --image myimage:latest --language python --test-framework pytest
```

### 使用 --image 显式指定镜像名

当路径结构不符合 `{config_name}/{target}` 格式时，需要使用 `--image` 显式指定镜像名：

```bash
# 例如路径是 .../BurntSushi_ripgrep_14.1.1_15.0.0/baseline_004/dockerfiles/base
# 自动推断会得到错误的 "dockerfiles/base:latest"
# 应该使用 --image 显式指定：
python validate_image.py \
    --image burntushi_ripgrep_14.1.1_15.0.0/base:latest

# 结合其他参数
python validate_image.py \
    --image burntushi_ripgrep_14.1.1_15.0.0/base:latest \
    --language rust \
    --test-framework cargo
```

### 重新构建镜像并验证

使用 `--force-rebuild` 在验证前重新构建 Docker 镜像。需要同时提供 `--dockerfile` 和 `--repo-path`：

```bash
# 完整的重建并验证流程
python validate_image.py \
    --image burntushi_ripgrep_14.1.1_15.0.0/base:latest \
    --force-rebuild \
    --dockerfile DATA/harness_workspace/BurntSushi_ripgrep_14.1.1_15.0.0/baseline_004/dockerfiles/base/Dockerfile \
    --repo-path DATA/github_data/repos/BurntSushi_ripgrep

# 重建后只做快速检查（跳过 Agent）
# 示例 1: Dockerfile 在 base/dockerfiles/ 目录下
python validate_image.py \
    --image burntushi_ripgrep_14.1.1_15.0.0/base:latest \
    --force-rebuild \
    --dockerfile DATA/harness_workspace/BurntSushi_ripgrep_14.1.1_15.0.0/base/dockerfiles/Dockerfile \
    --repo-path DATA/github_data/repos/BurntSushi_ripgrep \
    --skip-agent

# 示例 2: Dockerfile 在 baseline_xxx/dockerfiles/base/ 目录下
python validate_image.py \
    --image burntushi_ripgrep_14.1.1_15.0.0/base:latest \
    --force-rebuild \
    --dockerfile DATA/harness_workspace/BurntSushi_ripgrep_14.1.1_15.0.0/baseline_004/dockerfiles/base/Dockerfile \
    --repo-path DATA/github_data/repos/BurntSushi_ripgrep \
    --skip-agent

# 重建后使用 Agent 深度验证
python validate_image.py \
    --image urllib3_urllib3_2.0.6_2.3.0/base:latest \
    --force-rebuild \
    --dockerfile DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/base/Dockerfile \
    --repo-path DATA/github_data/repos/urllib3_urllib3 \
    --model sonnet
```

**注意**：`--force-rebuild` 必须同时提供：
- `--dockerfile`: Dockerfile 的完整路径
- `--repo-path`: 仓库路径（作为 docker build 的 context）

### 保存结果

```bash
python validate_image.py --work-dir ... --output validation_result.json
```

## 支持的语言/框架组合

| 语言 | 框架 |
|------|------|
| Python | pytest, unittest |
| Java | maven, gradle |
| JavaScript | jest, mocha |
| Go | go_test |
| Rust | cargo |

## 输出结果

### 状态码

| 状态 | 含义 | 退出码 |
|------|------|--------|
| `PASS` | 所有检查通过 | 0 |
| `WARNING` | 部分检查有警告 | 0 |
| `FAIL` | 检查失败 | 1 |

### JSON 输出格式

```json
{
  "status": "PASS",
  "image_name": "myproject/base:latest",
  "language": "python",
  "test_framework": "pytest",
  "checks": [
    {
      "name": "image_exists",
      "status": "PASS",
      "message": "Image found: abc123def456",
      "details": {"id": "abc123def456", "size": 1234567890}
    }
  ],
  "summary": {"test_count": 42},
  "recommendations": [],
  "duration": 15.23,
  "timestamp": "2024-01-15T10:30:00.000000"
}
```

