zhipu模型访问端点为：http://10.174.0.70:34000/v1/chat/completions

```cmd
curl -N -X POST "http://10.174.0.70:34000/v1/chat/completions"   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-vgeocsgvmkutmzivcrkaiyxxjygwcazjozlgkpyqmamovclv"   -d '{
    "model": "GLM-5.3-QJ", 
    "messages": [
      {"role": "user", "content": "你好，请介绍你自己"}
    ],
    "stream": true
  }'
```