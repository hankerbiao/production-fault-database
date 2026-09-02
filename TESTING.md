# 测试指南

## 本地快速检查

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install -r requirements-dev.txt
.venv-test/bin/pytest -q

cd backend
go test ./...
go vet ./...

cd ../frontend
npm ci
npm test -- --run
npm run build
```

## MongoDB 集成测试

Go 集成测试通过 `MONGO_TEST_URI` 启用，并使用随机集合名，测试结束自动删除集合：

```bash
MONGO_TEST_URI=mongodb://127.0.0.1:27017 go test ./... -run TemporaryDatabase
```

Python 同步测试默认使用 fake HTTP、HANA 和 Mongo doubles，不会连接外部服务。需要真实临时 MongoDB 时，应在 CI 或本地 Testcontainer 中提供测试 URI，禁止复用项目根目录 `.env`。

## E2E

```bash
cd frontend
npx playwright install chromium
npm run test:e2e
```

E2E 会启动 Vite，并拦截 `/api/*` 返回固定脱敏数据，分别执行桌面和移动视口的维修记录、详情和订单筛选旅程。

## CI 门禁

`.github/workflows/test.yml` 包含 Python、Go、前端和 E2E 四个 Job。E2E 只安装 Chromium；测试报告、失败截图和 trace 作为 artifact 保存。所有测试均不读取生产凭据或连接生产 SAP/MongoDB。
