import os
"""Local embedding search. Hybrid retrieval stays local: the only network call
is to a loopback embeddings endpoint (Ollama /api/embed or OpenAI
/v1/embeddings); a missing endpoint falls back to lexical-only retrieval."""
import array, hashlib, json, math, sqlite3, time, urllib.request
MODEL = os.environ.get('SHARED_VAULT_EMBED_MODEL', 'bge-m3:latest')
# Default is the stock Ollama port; point SHARED_VAULT_EMBED_URL elsewhere if
# embeddings live on a dedicated Ollama instance (so they never evict the chat model).
URL = os.environ.get('SHARED_VAULT_EMBED_URL', 'http://127.0.0.1:11434/api/embed')

def tunable(name, default):
    # Read at call time so a hook request's 'tune' overrides take effect per query.
    try: return float(os.environ.get(name) or default)
    except ValueError: return float(default)

def embed(texts, timeout=30):
    req = urllib.request.Request(os.environ.get('SHARED_VAULT_EMBED_URL') or URL,
        data=json.dumps({'model': os.environ.get('SHARED_VAULT_EMBED_MODEL') or MODEL, 'input': texts,
        'truncate': True, 'keep_alive': '5m'}).encode(), headers={'Content-Type':'application/json'})
    # Ignore proxy settings: the only permitted embedding destination is loopback.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
        data = json.load(r)
    if 'embeddings' in data: vectors = data['embeddings']                       # Ollama shape
    elif 'data' in data: vectors = [item['embedding'] for item in data['data']]  # OpenAI shape
    else: raise ValueError('Unsupported embeddings response shape')
    if len(vectors) != len(texts): raise ValueError('Embedding count mismatch')
    for v in vectors:
        if not v or not all(math.isfinite(x) for x in v): raise ValueError('Invalid embedding')
    return vectors

def connect(v):
    db = sqlite3.connect(v.cache/'vectors.sqlite3', timeout=10)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, model TEXT, path TEXT, hash TEXT, start INTEGER, end INTEGER, heading TEXT, vector BLOB)')
    return db

def windows(v):
    for path, doc in v.docs.items():
        for c in doc['chunks']:
            lines = c['text'].splitlines(); offset = 0
            while offset < len(lines):
                end = offset; size = 0
                while end < len(lines) and (size < 1800 or end == offset):
                    size += len(lines[end])+1; end += 1
                body = '\n'.join(lines[offset:end])[:6000]
                if body.strip():
                    text = path+'\n'+c['heading']+'\n'+body
                    key = hashlib.sha256((MODEL+'\0'+path+'\0'+doc['hash']+'\0'+str(c['start']+offset)).encode()).hexdigest()
                    yield key, path, doc['hash'], c['start']+offset, c['start']+end-1, c['heading'], text
                offset = end

def build(v):
    import fcntl
    with (v.cache/'vector-index.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        v.refresh(); rows = list(windows(v))
        with connect(v) as db:
            present = {r[0] for r in db.execute('SELECT key FROM vectors WHERE model=?',(MODEL,))}
            pending = [r for r in rows if r[0] not in present]
            print(json.dumps({'total':len(rows),'pending':len(pending)}), flush=True)
            for i in range(0,len(pending),8):
                batch = pending[i:i+8]; vectors = embed([r[-1] for r in batch], timeout=120)
                for r, vec in zip(batch,vectors):
                    db.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?,?,?,?)',
                        (r[0],MODEL,*r[1:6],array.array('f',vec).tobytes()))
                db.commit()
                if i%80==0: print(json.dumps({'indexed':min(i+8,len(pending)),'of':len(pending)}),flush=True)
            active = {r[0] for r in rows}
            stale = [(k,) for k, in db.execute('SELECT key FROM vectors') if k not in active]
            db.executemany('DELETE FROM vectors WHERE key=?', stale); db.commit()
        print(json.dumps({'complete':True,'chunks':len(rows)}), flush=True)

_matrix_cache = {}  # persistent-daemon: keep vectors in RAM across queries; key = dbfile mtime

def search(v, query, scope, limit):
    min_sim = tunable('SHARED_VAULT_MIN_SIMILARITY', 0.38)
    dbfile=v.cache/'vectors.sqlite3'
    if not dbfile.exists(): return [], 'not_indexed'
    try:
        import numpy as np
    except ImportError:
        np = None
    key = (str(dbfile), dbfile.stat().st_mtime_ns)
    if np is not None and _matrix_cache.get('key') == key:
        records_all, mat = _matrix_cache['records'], _matrix_cache['mat']
        keep = [i for i, r in enumerate(records_all)
                if r[0] in v.docs and v.docs[r[0]]['hash'] == r[1]
                and (not scope or r[0].startswith(scope.strip('/') + '/'))]
        if not keep: return [], 'not_indexed'
        sel = np.asarray(keep)
        records = [records_all[i] for i in keep]
        mat = mat[sel]
    else:
        with sqlite3.connect('file:'+str(dbfile)+'?mode=ro',uri=True) as db:
            records = db.execute('SELECT path,hash,start,end,heading,vector FROM vectors WHERE model=?',(MODEL,)).fetchall()
        records = [r for r in records if r[0] in v.docs and v.docs[r[0]]['hash']==r[1] and (not scope or r[0].startswith(scope.strip('/')+'/'))]
        if not records: return [], 'not_indexed'
        if np is not None:
            mat = np.frombuffer(b''.join(r[5] for r in records), dtype=np.float32).reshape(len(records), -1)
            # Cache the UNfiltered matrix so scope/hash churn doesn't force reloads.
            with sqlite3.connect('file:'+str(dbfile)+'?mode=ro',uri=True) as db:
                all_recs = db.execute('SELECT path,hash,start,end,heading,vector FROM vectors WHERE model=?',(MODEL,)).fetchall()
            _matrix_cache.clear()
            _matrix_cache.update(key=key, records=[r[:5] for r in all_recs],
                                 mat=np.frombuffer(b''.join(r[5] for r in all_recs), dtype=np.float32).reshape(len(all_recs), -1))
    q = embed([query], timeout=5)[0]
    if np is not None:
        if mat.shape[1] != len(q): return [], 'not_indexed'
        qv = np.asarray(q, dtype=np.float32)
        denom = np.linalg.norm(qv) * np.linalg.norm(mat, axis=1)
        scores = (mat @ qv) / np.where(denom == 0, 1, denom)
        top = np.argsort(-scores)[:limit * 3 * 4]
        scored = [(float(scores[i]), *records[i][:5]) for i in top if scores[i] >= min_sim]
    else:
        norm = math.sqrt(sum(x*x for x in q)); scored=[]
        for path,sha,start,end,heading,blob in records:
            vec=array.array('f');vec.frombytes(blob)
            if len(vec)!=len(q): continue
            denom=norm*math.sqrt(sum(x*x for x in vec))
            score=sum(a*b for a,b in zip(q,vec))/denom if denom else 0
            if score>=min_sim: scored.append((score,path,sha,start,end,heading))
    scored.sort(reverse=True); results=[]
    for score,path,sha,start,end,heading in scored[:limit*3]:
        # Read through the existing path and freshness guards.
        try: item=v.read(path,start,min(end,start+199),expected_hash=sha)
        except (ValueError,OSError): continue
        item.update(heading=heading,section_start=start,section_end=end,excerpt=True,semantic_score=round(score,4))
        results.append(item)
        if len(results)>=limit: break
    return results, 'ready'

def enable(cls):
    lexical=cls.query
    def query(self,query,scope=None,limit=4,max_chars=6000):
        rrf_k=tunable('SHARED_VAULT_RRF_K',60)
        limit=max(1,min(int(limit),8));max_chars=max(500,min(int(max_chars),16000))
        result=lexical(self,query,scope,8,16000)
        try: semantic,status=search(self,query,scope,8)
        except Exception as e: semantic,status=[], 'lexical_fallback:'+type(e).__name__
        # Reciprocal-rank fusion retains exact keyword matches and adds paraphrases.
        ranks={};items={}
        for hits in (result['results'],semantic):
            for rank,item in enumerate(hits):
                key=(item['path'],item.get('section_start',item['start']))
                ranks[key]=ranks.get(key,0)+1/(rrf_k+rank+1);items.setdefault(key,item)
        output=[]; used=0
        for key in sorted(ranks,key=lambda k:-ranks[k]):
            item=dict(items[key]);budget=min(3000,max_chars-used)
            if budget<250 or len(output)>=limit:break
            lines=item['text'].splitlines();kept=[];size=0
            for line in lines:
                if size+len(line)+1>budget:break
                kept.append(line);size+=len(line)+1
            if not kept:continue
            item['text']='\n'.join(kept);item['end']=item['start']+len(kept)-1
            item['excerpt']=item.get('excerpt',False) or len(kept)<len(lines)
            output.append(item);used+=size
        result.update(results=output,body_characters=used,retrieval='hybrid' if status=='ready' else 'lexical',semantic_status=status)
        return result
    cls.query=query
