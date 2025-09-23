# OfferMoreMore
七牛云比赛-offer多多队

# AI 3D 单体生成器（Flask + HTML）

本项目为一个示例网页应用，支持：
- 文本或图片生成单个 3D 模型（采用 Provider 适配层，可替换为 Meshy/Kaedim/TripoSR 等）
- 三维模型 OBJ 在线预览（Three.js）
- 效果评估系统（评分、问题标签、KPI 统计）
- 模型调用频次优化（缓存命中、相似提示词复用）

## 本地运行

```bash
pip install -r requirements.txt
python app.py
# 浏览器打开 http://localhost:7860
```

