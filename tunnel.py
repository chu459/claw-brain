"""SSH Port Forwarding: local:8000 -> AutoDL:8000
Direct socket listener approach (more reliable on Windows)
"""
import paramiko
import os
import select
import socket
import threading
import sys
import time

SSH_HOST = os.environ.get("AUTODL_SSH_HOST", "connect.bjb1.seetacloud.com")
SSH_PORT = int(os.environ.get("AUTODL_SSH_PORT", "48216"))
SSH_USER = os.environ.get("AUTODL_SSH_USER", "root")
SSH_PASS = os.environ.get("AUTODL_SSH_PASS", "")
LOCAL_PORT = int(os.environ.get("AUTODL_LOCAL_PORT", "8001"))
REMOTE_HOST = os.environ.get("AUTODL_REMOTE_HOST", "127.0.0.1")
REMOTE_PORT = int(os.environ.get("AUTODL_REMOTE_PORT", "8000"))


def forward(transport, local_port, remote_host, remote_port):
    """Create local socket server and forward each connection via SSH"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local_port))
    server.listen(5)

    def handle(client_sock):
        peer = client_sock.getpeername()
        try:
            chan = transport.open_channel(
                "direct-tcpip",
                (remote_host, remote_port),
                peer,
            )
            if chan is None:
                sys.stderr.write(f"[tunnel] open_channel returned None for {peer}\n")
                sys.stderr.flush()
                client_sock.close()
                return
        except Exception as e:
            sys.stderr.write(f"[tunnel] open_channel failed for {peer}: {e}\n")
            sys.stderr.flush()
            client_sock.close()
            return

        sys.stderr.write(f"[tunnel] Connected {peer} -> {remote_host}:{remote_port}\n")
        sys.stderr.flush()

        try:
            while True:
                r, w, x = select.select([client_sock, chan], [], [], 1)
                if client_sock in r:
                    data = client_sock.recv(4096)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(4096)
                    if not data:
                        break
                    client_sock.sendall(data)
        except Exception as e:
            sys.stderr.write(f"[tunnel] Error on {peer}: {e}\n")
            sys.stderr.flush()
        finally:
            chan.close()
            client_sock.close()

    while True:
        try:
            client_sock, addr = server.accept()
            t = threading.Thread(target=handle, args=(client_sock,))
            t.daemon = True
            t.start()
        except OSError:
            break


def main():
    if not SSH_PASS:
        raise RuntimeError("Please set AUTODL_SSH_PASS before starting the tunnel.")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASS)
    sys.stdout.write(f"Tunnel active: 127.0.0.1:{LOCAL_PORT} -> AutoDL:{REMOTE_PORT}\n")
    sys.stdout.flush()
    try:
        forward(client.get_transport(), LOCAL_PORT, REMOTE_HOST, REMOTE_PORT)
    except KeyboardInterrupt:
        pass
    client.close()


if __name__ == "__main__":
    main()
