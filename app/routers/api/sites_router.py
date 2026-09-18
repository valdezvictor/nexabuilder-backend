"""Multi-site management router"""
from fastapi import APIRouter, Header, HTTPException
import os, re as _re
from sqlalchemy import create_engine, text as sqlt
from sqlalchemy.orm import sessionmaker
def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()



import boto3, re

router = APIRouter(prefix="/sites", tags=["sites"])
ADMIN_KEY = "GidhUSbSVmhSzpY8Xd7gfBEJJYB-ycHKz5j-JxEYSpU"

def _auth(k):
    if k!=ADMIN_KEY: raise HTTPException(403,"Invalid admin key")

SITE_BUCKETS={"nexabuilder":"nexabuilder-root-site-979841141166-us-west-1-an","unapiscina":"unapiscina-frontend","eelectricista":"eelectricista.com","piscinasy":"piscinasy.com","losruferos":"losruferos.com","ijardinero":"ijardinero.com","swimmingpul":"swimmingpul.com"}
SITE_CF={"unapiscina":"EIUC99BH83ACL","eelectricista":"E2INQNE25T38VT","piscinasy":"E10IYVR79GRZWN","losruferos":"E3OXMEVG6WNOUQ","ijardinero":"E2VBXFVQBMK986","nexabuilder":"EDLQAZ1IS2WIG"}
DOMAIN_SLUG={"nexabuilder.com":"nexabuilder","unapiscina.com":"unapiscina","eelectricista.com":"eelectricista","piscinasy.com":"piscinasy","losruferos.com":"losruferos","ijardinero.com":"ijardinero","swimmingpul.com":"swimmingpul"}

@router.get("")
async def list_sites(x_admin_key:str=Header(...)):
    _auth(x_admin_key)
    db=_db()
    try:
        rows=db.execute(sqlt("SELECT id,name,domain,type FROM tenants ORDER BY type,name")).fetchall()
        sites=[]
        for row in rows:
            d=dict(row._mapping)
            slug=DOMAIN_SLUG.get(d["domain"],d["domain"].split(".")[0])
            bc=db.execute(sqlt("SELECT COUNT(*) FROM content_blocks WHERE tenant_id=:s"),{"s":slug}).fetchone()[0]
            ac=db.execute(sqlt("SELECT COUNT(*) FROM blog_articles WHERE site_id=:s"),{"s":slug}).fetchone()[0]
            d["slug"]=slug; d["block_count"]=int(bc); d["article_count"]=int(ac); d["has_bucket"]=slug in SITE_BUCKETS
            sites.append(d)
        return {"sites":sites}
    finally:
        db.close()

@router.get("/{tenant_id}/blocks")
async def get_blocks(tenant_id:str,page_slug:str=None,x_admin_key:str=Header(...)):
    _auth(x_admin_key)
    db=_db()
    try:
        q="SELECT * FROM content_blocks WHERE tenant_id=:tid"
        p={"tid":tenant_id}
        if page_slug: q+=" AND page_slug=:slug"; p["slug"]=page_slug
        q+=" ORDER BY page_slug,block_key"
        rows=db.execute(sqlt(q),p).fetchall()
        return {"tenant_id":tenant_id,"blocks":[dict(r._mapping) for r in rows],"total":len(rows)}
    finally:
        db.close()

@router.put("/{tenant_id}/blocks/{page_slug}/{block_key}")
async def update_block(tenant_id:str,page_slug:str,block_key:str,payload:dict,x_admin_key:str=Header(...)):
    _auth(x_admin_key)
    db=_db()
    try:
        db.execute(sqlt("INSERT INTO content_blocks (tenant_id,page_slug,block_key,content_type,value,is_published,version,updated_by) VALUES (:tid,:slug,:key,:ct,:val,:pub,1,'admin') ON CONFLICT (tenant_id,page_slug,block_key) DO UPDATE SET value=EXCLUDED.value,is_published=EXCLUDED.is_published,updated_at=NOW(),version=content_blocks.version+1,updated_by='admin'"),{"tid":tenant_id,"slug":page_slug,"key":block_key,"ct":payload.get("content_type","text"),"val":payload.get("value",""),"pub":payload.get("is_published",True)})
        db.commit()
        return {"ok":True,"tenant_id":tenant_id,"page_slug":page_slug,"block_key":block_key}
    finally:
        db.close()

@router.post("/{tenant_id}/push-legal")
async def push_legal(tenant_id:str,x_admin_key:str=Header(...)):
    _auth(x_admin_key)
    db=_db(); s3=boto3.client("s3",region_name="us-west-1")
    bucket=SITE_BUCKETS.get(tenant_id)
    if not bucket: raise HTTPException(400,f"No bucket for {tenant_id}")
    try:
        priv=db.execute(sqlt("SELECT value FROM content_blocks WHERE tenant_id=:t AND page_slug='shared' AND block_key='privacy_policy'"),{"t":tenant_id}).fetchone()
        terms=db.execute(sqlt("SELECT value FROM content_blocks WHERE tenant_id=:t AND page_slug='shared' AND block_key='terms_of_service'"),{"t":tenant_id}).fetchone()
        if not priv or not terms: raise HTTPException(404,"Legal blocks not found")
        nr=db.execute(sqlt("SELECT name FROM tenants WHERE domain=:d"),{"d":tenant_id+".com"}).fetchone()
        name=nr[0] if nr else tenant_id
        results=[]
        for slug,label,content in [("privacy","Privacy Policy",priv[0]),("terms","Terms of Service",terms[0])]:
            try:
                existing=s3.get_object(Bucket=bucket,Key=f"{slug}/index.html")["Body"].read().decode()
                page=re.sub(r"<title>.*?</title>",f"<title>{label} | {name}</title>",existing,flags=re.S|re.I,count=1)
                mm=re.search(r"(<main[^>]*>)(.*?)(</main>)",page,re.S|re.I)
                if mm: page=page[:mm.start(2)]+content+page[mm.end(2):]
            except Exception:
                page=(f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>{label} | {name}</title>'
                      f'<style>body{{font-family:Inter,sans-serif;max-width:860px;margin:40px auto;padding:0 24px;color:#1a2332;line-height:1.7}}</style></head>'
                      f'<body><h1>{label}</h1><main>{content}</main></body></html>')
            s3.put_object(Bucket=bucket,Key=f"{slug}/index.html",Body=page.encode("utf-8"),ContentType="text/html",CacheControl="public, max-age=3600")
            results.append({"slug":slug,"size":len(page),"ok":True})
        return {"tenant_id":tenant_id,"bucket":bucket,"pushed":results}
    finally:
        db.close()

@router.post("/push-legal-all")
async def push_legal_all(x_admin_key:str=Header(...)):
    _auth(x_admin_key)
    results=[]
    for tid in SITE_BUCKETS.keys():
        try:
            r=await push_legal(tid,x_admin_key=x_admin_key)
            results.append({"tenant_id":tid,"ok":True,"pushed":len(r["pushed"])})
        except Exception as e:
            results.append({"tenant_id":tid,"ok":False,"error":str(e)[:80]})
    return {"results":results}

@router.get("/{tenant_id}/articles")
async def get_site_articles(tenant_id:str,x_admin_key:str=Header(...)):
    _auth(x_admin_key)
    db=_db()
    try:
        rows=db.execute(sqlt("SELECT id,site_id,slug,h1,seo_title,status,published_at,word_count,meta_description,category FROM blog_articles WHERE site_id=:s ORDER BY created_at DESC"),{"s":tenant_id}).fetchall()
        return {"tenant_id":tenant_id,"articles":[dict(r._mapping) for r in rows],"total":len(rows)}
    finally:
        db.close()

@router.get("/{tenant_id}/gsc")
async def get_site_gsc(tenant_id:str,x_admin_key:str=Header(...)):
    _auth(x_admin_key)
    db=_db()
    DMAP={"nexabuilder":"nexabuilder.com","unapiscina":"unapiscina.com","eelectricista":"eelectricista.com","piscinasy":"piscinasy.com","losruferos":"losruferos.com","ijardinero":"ijardinero.com","swimmingpul":"swimmingpul.com"}
    domain=DMAP.get(tenant_id,tenant_id+".com")
    try:
        s=db.execute(sqlt("SELECT COALESCE(SUM(impressions),0) as impressions,COALESCE(SUM(clicks),0) as clicks,COALESCE(ROUND(AVG(ctr)::numeric*100,2),0) as avg_ctr,COALESCE(ROUND(AVG(position)::numeric,1),0) as avg_pos FROM gsc_keywords WHERE page LIKE :d"),{"d":f"%{domain}%"}).fetchone()
        tq=db.execute(sqlt("SELECT query,SUM(impressions) as imp,SUM(clicks) as cli,ROUND(AVG(position)::numeric,1) as pos FROM gsc_keywords WHERE page LIKE :d AND query IS NOT NULL GROUP BY query ORDER BY imp DESC LIMIT 10"),{"d":f"%{domain}%"}).fetchall()
        return {"tenant_id":tenant_id,"domain":domain,"summary":dict(s._mapping) if s else {},"top_queries":[dict(r._mapping) for r in tq]}
    finally:
        db.close()
