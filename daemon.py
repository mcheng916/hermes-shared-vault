#!/usr/bin/env python3
"""Persistent retrieval daemon for shared-vault hooks.

Holds the lexical index and the vector matrix in RAM so each hook query costs
embed (~0.2s) + matmul (~0.07s) instead of a full process cold start (~2.5s).
Loopback/local-only: Unix socket, mode 0700 directory, no network listener.

Protocol: one JSON request per line -> one JSON response per line.
  {"cmd":"query","text":...,"limit":...,"max_chars":...,"hook":"hermes-hook"|"claude-hook"}
  -> {"ok":true,"result":{...}}  or  {"ok":false,"error":"..."}
"""
import json, os, selectors, socket, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault as V

SOCK_TIMEOUT = 20.0   # generous: first query after an index rebuild rebuilds the matrix
IDLE_REFRESH = 120.0  # keep lexical index and matrix warm between queries


def serve(v):
    sock_path = v.cache / 'daemon.sock'
    v.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.connect(str(sock_path))
        probe.close()
        # Another instance owns the socket. Sleep so a KeepAlive supervisor
        # doesn't busy-restart; re-check on next launch once the owner is gone.
        print('daemon already running; standing down', file=sys.stderr, flush=True)
        time.sleep(3600)
        return 0
    except OSError:
        pass
    if sock_path.exists():
        sock_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    os.chmod(str(sock_path), 0o600)
    server.listen(16)
    server.setblocking(False)

    def run_hook(req):
        text = req.get('text', '')
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError('invalid query')
        limit = int(req.get('limit', 4)); max_chars = int(req.get('max_chars', 6000))
        result = v.query(text, limit=limit, max_chars=max_chars)
        hook = req.get('hook', '')
        if hook == 'claude-hook':
            # Same inbox-staleness filter the CLI hook applies.
            cutoff = time.strftime('%Y%m%d', time.localtime(time.time() - 14 * 86400))
            kept = []; used = 0
            for item in result['results']:
                name = item['path'].rsplit('/', 1)[-1]
                if '/inbox/' in item['path'] and name[:8].isdigit() and name[:8] < cutoff:
                    continue
                if len(kept) >= 2 or used >= 2250:
                    break
                item['text'] = item['text'][:2500 - used]; used += len(item['text']); kept.append(item)
            result['results'] = kept; result['body_characters'] = used
        if hook == 'hermes-hook':
            for item in result['results']:
                item['source_path'] = str(v.checked(item['path']))
        return result

    sel = selectors.DefaultSelector()
    sel.register(server, selectors.EVENT_READ)
    clients = {}  # conn -> buffer
    last_refresh = time.monotonic()
    print('shared-vault daemon ready ' + str(sock_path), file=sys.stderr, flush=True)
    try:
        while True:
            timeout = max(0.0, IDLE_REFRESH - (time.monotonic() - last_refresh))
            events = sel.select(timeout)
            now = time.monotonic()
            if now - last_refresh >= IDLE_REFRESH:
                try:
                    v.refresh()
                except Exception:
                    pass
                last_refresh = now
            for key, _ in events:
                if key.fileobj is server:
                    try:
                        conn, _ = server.accept()
                        conn.setblocking(False)
                        sel.register(conn, selectors.EVENT_READ)
                        clients[conn] = ''
                    except OSError:
                        pass
                    continue
                conn = key.fileobj
                try:
                    data = conn.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b''
                if not data:
                    sel.unregister(conn); conn.close(); clients.pop(conn, None)
                    continue
                clients[conn] += data.decode('utf-8', 'replace')
                while '\n' in clients[conn]:
                    line, clients[conn] = clients[conn].split('\n', 1)
                    if not line.strip():
                        continue
                    try:
                        req = json.loads(line)
                        payload = {'ok': True, 'result': run_hook(req)}
                    except Exception as e:
                        payload = {'ok': False, 'error': type(e).__name__ + ': ' + str(e)[:200]}
                    try:
                        conn.sendall(json.dumps(payload, ensure_ascii=False).encode() + b'\n')
                    except OSError:
                        pass
    finally:
        sel.close(); server.close()
        try:
            sock_path.unlink()
        except OSError:
            pass


def main():
    os.umask(0o077)
    v = V.Vault(os.environ.get('SHARED_VAULT_ROOT'))
    v.refresh()
    # Pre-warm the full path (loads vectors + numpy matrix, wakes bge-m3) before accepting.
    try:
        v.query('warm up', limit=1, max_chars=500)
    except Exception:
        pass
    return serve(v)


if __name__ == '__main__':
    sys.exit(main())
