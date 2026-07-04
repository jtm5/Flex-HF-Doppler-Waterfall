import socket
import threading

def start_telnet_client(host, port, on_message, on_disconnect=None):
    """
    Start a two-way Telnet-style TCP client.

    Parameters:
        host (str): Server hostname or IP.
        port (int): Server port.
        on_message (callable): Function called with each received line.
        on_disconnect (callable): Optional function called when server disconnects.

    Returns:
        send_func (callable): Use send_func("text") to send data.
        stop_func (callable): Call stop_func() to close connection.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    running = True

    def recv_loop():
        nonlocal running
        try:
            while running:
                data = sock.recv(4096)
                if not data:
                    running = False
                    if on_disconnect:
                        on_disconnect()
                    break

                # Split on CRLF boundaries
                for line in data.decode().splitlines():
                    if line.strip():
                        on_message(line.rstrip())
        except Exception:
            running = False
            if on_disconnect:
                on_disconnect()

    thread = threading.Thread(target=recv_loop, daemon=True)
    thread.start()

    def send_func(msg):
        """Send a Telnet-style CRLF terminated message."""
        if running:
            sock.sendall((msg + "\r\n").encode())

    def stop_func():
        """Stop the client and close the socket."""
        nonlocal running
        running = False
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        sock.close()

    return send_func, stop_func





# from telnet_client import start_telnet_client

def handle_message(msg):
    print(f"[SERVER] {msg}")

def handle_disconnect():
    print("Server disconnected.")

send, stop = start_telnet_client(
    host="10.0.0.252",
    port=4992,
    on_message=handle_message,
    on_disconnect=handle_disconnect
)

# # Now you can send messages from anywhere in your script:
# send("c1| sub pan all\r\n")
# msg = input("> ")
# send(msg)
# while True:
#     msg = input("> ")
#     if msg.lower() in ("exit", "quit"):
#         break
#     send(msg)

# # Later, when shutting down:
# # stop()
