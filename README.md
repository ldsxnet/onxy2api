# onyx2api (Python + FastAPI)

将原始 Go 版本重写为 Python/FastAPI，并提供本地配置文件与 Web UI 管理界面。

## 功能

- 兼容 OpenAI 接口：`POST /v1/chat/completions`
- 兼容 Anthropic 接口：`POST /v1/messages`
- 模型列表：`GET /v1/models`
- 健康检查：`GET /health`
- 本地配置管理 API：
  - `GET /api/config`
  - `POST /api/config`
- Web UI：`GET /ui`

## 本地配置

配置保存于项目根目录的 `config.json`（首次启动自动创建）。

字段说明：

- `onyx_base`: Onyx 基础地址（默认 `https://cloud.onyx.app`）
- `onyx_keys`: Onyx Token 列表（轮询）
- `client_api_keys`: 客户端访问本服务的 API Key 列表（可选）
- `default_persona`: 默认 persona_id
- `default_model`: 默认模型
- `request_timeout_seconds`: 请求超时秒数
- `admin_password`: 管理页面密码（首次启动自动生成随机密码）

## 管理页面鉴权

- 管理页面配置接口 `GET/POST /api/config` 需要管理密码。
- 前端会通过 `X-Admin-Password` 请求头提交密码。
- 首次启动会自动生成随机密码并写入 `config.json`，同时在启动日志中打印。
- 可在管理页面输入“新管理密码”后保存，即可修改。

## 快速启动

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 19898
```

服务默认监听 `http://127.0.0.1:19898`。

打开 Web UI：

- `http://127.0.0.1:19898/ui`
