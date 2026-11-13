# 贡献指南

## 环境准备

```powershell
python -m pip install -r requirements.txt
cd frontend
npm install
cd ..
```

## 运行测试

提交前请确保以下测试全部通过：

```powershell
# 后端（494 个）
python -m unittest discover -s tests

# 控制台后端（93 个）
cd frontend
python -m unittest tests.test_server

# 前端（14 个）
npm test
cd ..
```

## 代码规范

- Python：4 空格缩进，遵循 PEP 8；类型注解尽量完整；
- TypeScript/React：2 空格缩进，组件与工具函数分离；
- 配置与文档：UTF-8、LF 换行，由 `.editorconfig` 统一；
- 不提交运行时数据：`frontend/runtime-local/`、`reports/`、`logs/`、`dist/`、`node_modules/`、`*.apk`。

## 提交信息

采用 Conventional Commits 前缀：

| 前缀 | 用途 |
|---|---|
| `feat:` | 新功能 |
| `fix:` | 缺陷修复 |
| `perf:` | 性能优化 |
| `refactor:` | 不改变行为的重构 |
| `test:` | 测试相关 |
| `docs:` | 文档 |
| `chore:` | 构建/依赖/杂项 |

示例：

```
fix(prepare): wait for a lingering installer window instead of failing the cycle
feat(install): answer vendor install prompts automatically for every device
```

## 提交前自查

- [ ] 新增或修改行为有对应测试；
- [ ] 不包含设备序列号、账号、密钥、公司内网地址；
- [ ] 不包含 `runtime-local/`、日志、截图、APK 等运行数据；
- [ ] README 或配置说明已同步更新。
