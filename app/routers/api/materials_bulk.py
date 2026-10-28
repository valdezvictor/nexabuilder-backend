"""materials_bulk.py - Bulk image upload + staging queue for Materials Catalog"""
import os, uuid, json
from typing import List, Optional
from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import text as sqlt
import boto3
import anthropic as _ant

router = APIRouter(prefix="/api/materials", tags=["Materials Bulk"])
_ALLOWED = {"image/jpeg","image/png","image/webp","image/avif","image/gif"}
_MAX_MB  = 12
_MAXF    = 50
_S3C     = None

def _adm(k):
    key = os.getenv("CMS_ADMIN_KEY","")
    if not key or k != key: raise HTTPException(403,"Invalid admin key")

def _bkt(): return os.getenv("MEDIA_BUCKET","nexabuilder-root-site-979841141166-us-west-1-an")

def _s3():
    global _S3C
    if _S3C is None: _S3C = boto3.client("s3",region_name="us-west-1")
    return _S3C

def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import os as _os
    engine = create_engine(
        _os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),
        pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()

def _products(db):
    rows = db.execute(sqlt("SELECT id,slug,display_name,category FROM materials_catalog ORDER BY category,display_name")).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/bulk-upload")
async def bulk_upload(
    files: List[UploadFile]=File(...),
    description_raw: Optional[str]=Form(None),
    target_slug: Optional[str]=Form(None),
    image_role: Optional[str]=Form("gallery"),
    x_admin_key: str=Header(...),
):
    _adm(x_admin_key)
    if len(files)>_MAXF: raise HTTPException(422,f"Max {_MAXF} files per batch.")
    db=_db(); ok=[]; err=[]
    for f in files:
        mime=f.content_type or "image/jpeg"
        if mime not in _ALLOWED:
            err.append({"file":f.filename,"error":"Unsupported type: "+mime}); continue
        data=await f.read()
        if len(data)>_MAX_MB*1024*1024:
            err.append({"file":f.filename,"error":f"Too large (max {_MAX_MB}MB)"}); continue
        fname=(f.filename or "upload").replace(" ","_")
        key=f"materials/staging/{uuid.uuid4().hex}/{fname}"
        try:
            _s3().put_object(Bucket=_bkt(),Key=key,Body=data,ContentType=mime,CacheControl="max-age=31536000")
            url=f"https://{_bkt()}.s3.us-west-1.amazonaws.com/{key}"
        except Exception as e:
            err.append({"file":f.filename,"error":str(e)}); continue
        pid=None
        if target_slug:
            r=db.execute(sqlt("SELECT id FROM materials_catalog WHERE slug=:s LIMIT 1"),{"s":target_slug}).fetchone()
            if r: pid=r[0]
        row=db.execute(sqlt(
            "INSERT INTO materials_staging(s3_key,public_url,original_name,mime_type,file_size_bytes,description_raw,target_product_id,target_slug,image_role)"
            " VALUES(:k,:u,:n,:m,:sz,:d,:p,:sl,:r) RETURNING id,public_url,status"
        ),{"k":key,"u":url,"n":f.filename,"m":mime,"sz":len(data),"d":description_raw,"p":pid,"sl":target_slug,"r":image_role or "gallery"}).fetchone()
        db.commit()
        ok.append({"staging_id":row[0],"public_url":row[1],"status":row[2],"original_name":f.filename,"size_kb":round(len(data)/1024,1)})
    return {"uploaded":len(ok),"errors":len(err),"items":ok,"error_list":err}


@router.get("/staging")
async def get_staging(status: Optional[str]="pending", x_admin_key: str=Header(...)):
    _adm(x_admin_key)
    db=_db()
    w="WHERE ms.status=:status" if status!="all" else "WHERE 1=1"
    rows=db.execute(sqlt(f"""
        SELECT ms.id,ms.public_url,ms.original_name,ms.mime_type,ms.file_size_bytes,
               ms.description_raw,ms.description_en,ms.description_es,ms.lang_detected,
               ms.target_product_id,ms.target_slug,ms.image_role,ms.display_order,
               ms.status,ms.uploaded_at,mc.display_name AS product_name,mc.category AS product_category
        FROM materials_staging ms
        LEFT JOIN materials_catalog mc ON mc.id=ms.target_product_id
        {w} ORDER BY ms.uploaded_at DESC LIMIT 200
    """),{"status":status}).fetchall()
    return {"count":len(rows),"items":[dict(r._mapping) for r in rows],"products":_products(db)}


class StagingUpdate(BaseModel):
    description_raw:   Optional[str]=None
    description_en:    Optional[str]=None
    description_es:    Optional[str]=None
    target_product_id: Optional[int]=None
    target_slug:       Optional[str]=None
    image_role:        Optional[str]=None
    display_order:     Optional[int]=None

@router.put("/staging/{sid}")
async def update_staging(sid:int, payload:StagingUpdate, x_admin_key:str=Header(...)):
    _adm(x_admin_key)
    db=_db()
    pid=payload.target_product_id
    if payload.target_slug and not pid:
        r=db.execute(sqlt("SELECT id FROM materials_catalog WHERE slug=:s LIMIT 1"),{"s":payload.target_slug}).fetchone()
        if r: pid=r[0]
    db.execute(sqlt("""
        UPDATE materials_staging SET
          description_raw=COALESCE(:dr,description_raw),description_en=COALESCE(:den,description_en),
          description_es=COALESCE(:des,description_es),target_product_id=COALESCE(:pid,target_product_id),
          target_slug=COALESCE(:slg,target_slug),image_role=COALESCE(:rol,image_role),
          display_order=COALESCE(:ord,display_order)
        WHERE id=:id
    """),{"dr":payload.description_raw,"den":payload.description_en,"des":payload.description_es,
          "pid":pid,"slg":payload.target_slug,"rol":payload.image_role,"ord":payload.display_order,"id":sid})
    db.commit()
    return {"success":True,"staging_id":sid}


@router.post("/staging/{sid}/translate")
async def translate_desc(sid:int, x_admin_key:str=Header(...)):
    _adm(x_admin_key)
    db=_db()
    row=db.execute(sqlt("SELECT description_raw FROM materials_staging WHERE id=:id"),{"id":sid}).fetchone()
    if not row: raise HTTPException(404,"Not found")
    raw=(row[0] or "").strip()
    if not raw: raise HTTPException(422,"No description to translate.")
    try:
        client=_ant.Anthropic()
        msg=client.messages.create(model="claude-sonnet-4-6",max_tokens=600,messages=[{"role":"user","content":
            "You are a bilingual materials catalog assistant. Given this product description:\n"
            f"<description>{raw}</description>\n\n"
            "Detect language and produce clean EN+ES catalog descriptions (2-3 sentences each).\n"
            "Respond ONLY with valid JSON (no markdown):\n"
            '{"lang":"en or es","en":"english version","es":"spanish version"}'}])
        result=json.loads(msg.content[0].text.strip())
    except Exception as e:
        raise HTTPException(500,f"AI translation failed: {str(e)}")
    db.execute(sqlt("UPDATE materials_staging SET description_en=:en,description_es=:es,lang_detected=:l WHERE id=:id"),
               {"en":result.get("en",""),"es":result.get("es",""),"l":result.get("lang","unknown"),"id":sid})
    db.commit()
    return {"success":True,"staging_id":sid,"lang_detected":result.get("lang"),
            "description_en":result.get("en"),"description_es":result.get("es")}


def _do_approve(db, sid:int):
    result=db.execute(sqlt("""
        SELECT ms.id,ms.public_url,ms.original_name,ms.description_en,ms.description_es,
               ms.description_raw,ms.image_role,ms.display_order,ms.target_product_id,
               mc.gallery_images_meta
        FROM materials_staging ms
        LEFT JOIN materials_catalog mc ON mc.id=ms.target_product_id
        WHERE ms.id=:id
    """),{"id":sid}).fetchone()
    if not result: raise HTTPException(404,"Not found")
    # Use index-based access for psycopg2 row
    (rid,pub_url,orig_name,desc_en,desc_es,desc_raw,img_role,disp_order,target_pid,gal_meta) = result
    if not target_pid: raise HTTPException(422,"Map to a product first.")
    pid  = target_pid
    role = img_role or "gallery"
    if role=="hero":
        db.execute(sqlt("""
            UPDATE materials_catalog SET hero_image_url=:url,
              seo_description=COALESCE(:en,seo_description),
              seo_description_es=COALESCE(:es,seo_description_es),
              image_updated_at=NOW()::date,updated_at=NOW()
            WHERE id=:pid
        """),{"url":pub_url,"en":desc_en,"es":desc_es,"pid":pid})
    else:
        ex = gal_meta or []
        if isinstance(ex,str): ex=json.loads(ex)
        ex.append({"url":pub_url,
                   "caption_en":desc_en or desc_raw or "",
                   "caption_es":desc_es or "",
                   "display_order":disp_order or len(ex),
                   "original_name":orig_name})
        mj=json.dumps(ex)
        # Use cast() to avoid :: syntax issue with psycopg2
        db.execute(sqlt("""
            UPDATE materials_catalog
            SET gallery_images_meta=CAST(:m AS jsonb),
                gallery_images=(
                    SELECT ARRAY(SELECT elem->>'url'
                                 FROM jsonb_array_elements(CAST(:m2 AS jsonb)) elem
                                 ORDER BY (elem->>'display_order')::int)
                ),
                image_updated_at=NOW()::date,updated_at=NOW()
            WHERE id=:pid
        """),{"m":mj,"m2":mj,"pid":pid})
    db.execute(sqlt("UPDATE materials_staging SET status='approved',reviewed_at=NOW() WHERE id=:id"),{"id":sid})
    try:
        db.execute(sqlt("INSERT INTO materials_image_log(material_id,old_image,new_image,changed_by,notes) VALUES(:pid,NULL,:url,'bulk-upload',:n)"),
                   {"pid":pid,"url":pub_url,"n":"Bulk approved: "+(orig_name or "")})
    except: pass
    db.commit()
    return {"success":True,"staging_id":sid,"product_id":pid,"role":role,"url":pub_url}

@router.post("/staging/{sid}/approve")
async def approve_item(sid:int, x_admin_key:str=Header(...)):
    _adm(x_admin_key); return _do_approve(_db(),sid)

class BulkApprovePayload(BaseModel):
    staging_ids: List[int]

@router.post("/staging/bulk-approve")
async def bulk_approve(payload:BulkApprovePayload, x_admin_key:str=Header(...)):
    _adm(x_admin_key); db=_db(); ok=[]; err=[]
    for sid in payload.staging_ids:
        try: ok.append(_do_approve(db,sid))
        except HTTPException as e: err.append({"staging_id":sid,"error":e.detail})
        except Exception as e: err.append({"staging_id":sid,"error":str(e)})
    return {"approved":ok,"errors":err}

@router.delete("/staging/{sid}")
async def reject_item(sid:int, x_admin_key:str=Header(...)):
    _adm(x_admin_key); db=_db()
    row=db.execute(sqlt("SELECT s3_key FROM materials_staging WHERE id=:id"),{"id":sid}).fetchone()
    if not row: raise HTTPException(404,"Not found")
    try: _s3().delete_object(Bucket=_bkt(),Key=row[0])
    except: pass
    db.execute(sqlt("UPDATE materials_staging SET status='rejected' WHERE id=:id"),{"id":sid})
    db.commit()
    return {"success":True,"staging_id":sid}

@router.get("/catalog-full")
async def catalog_full(x_admin_key:str=Header(...)):
    """Full catalog with gallery metadata — used by Storyforge asset picker."""
    _adm(x_admin_key); db=_db()
    rows=db.execute(sqlt("""
        SELECT id,slug,category,display_name,hero_image_url,gallery_images,gallery_images_meta,
               seo_description,seo_description_es,stone_types,available_finishes,
               price_usd,unit,origin_region
        FROM materials_catalog ORDER BY category,display_name
    """)).fetchall()
    return {"count":len(rows),"items":[dict(r._mapping) for r in rows]}
