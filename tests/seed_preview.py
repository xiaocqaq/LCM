"""Seed explicit loopback preview with synthetic data only; no production credentials."""
import asyncio
import json
import httpx

async def main():
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8660',timeout=60) as c:
        r=await c.post('/api/v1/auth/ai-login',json={'username':'preview','password':'preview-only-test'})
        r.raise_for_status();c.headers['Authorization']='Bearer '+r.json()['accessToken']
        projects=[('memorys',205),('文档站',5),('交互课件',4),('',3)]
        titles=['项目架构与模块边界','上线检查与回退步骤','账号隔离与访问规则','检索评估与召回策略','待办事项与接手说明']
        saved=0
        for project,count in projects:
            for i in range(count):
                body={'title':f'{project or "未归项目"} · {titles[i%len(titles)]} {i+1:03}',
                      'project':project,'content':f'# {titles[i%len(titles)]}\n\n这是隔离验收生成的示例文档，不是生产知识。\n\n## 工作约束\n保留 Markdown 为准，避免覆盖并发修改。\n\n## 验收\n用于验证超过 200 篇时的分页、统计和导航。',
                      'type':['project_summary','decision','howto','fact','preference'][i%5],
                      'tags':['验收示例','非生产'],'importance':3,'source':'test:browser-preview'}
                r=await c.post('/api/v1/documents',json=body)
                if r.status_code==409: continue
                r.raise_for_status();saved+=1
            print('seeded',project or '未归项目',count,flush=True)
        result=(await c.get('/api/v1/projects')).json()
        print(json.dumps({'created':saved,'project_summary':result},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
