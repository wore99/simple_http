import socket 
import os
import pdb
import errno 
import struct
import pwd 
import json 
import simple_table 
#alias 
from select import *
from fcntl import *
from struct import unpack
from struct import pack
from cStringIO import StringIO 
from time import sleep 
from time import time


#server ip, port 
SERVER_IP = "127.0.0.1"
SERVER_PORT = 9988

MAX_LISTEN = 128
REMOTE = ("127.0.0.1", 9905) 

_AF_INET = socket.AF_INET
_SOCK_STREAM = socket.SOCK_STREAM
_SOL_SOCKET = socket.SOL_SOCKET
_SO_REUSEADDR = socket.SO_REUSEADDR
_SO_ERROR = socket.SO_ERROR
_O_NONBLOCK = os.O_NONBLOCK
_socket = socket.socket
_fromfd = socket.fromfd
_inet_ntop = socket.inet_ntop
_inet_aton = socket.inet_aton 

simple_table_translate = simple_table.translate
_accept = None

sockfd = None
ep = None
cons = {} 


f = open("SD.key", "r")
SD = {}
for k,v in json.loads(f.read()).items():
    SD[int(k)] = v
f.close()

f = open("DS.key", "r")
DS = {}
for k, v in json.loads(f.read()).items():
    DS[int(k)] = v
f.close()

SOCKS_HANDSHAKE_CLIENT = simple_table_translate(SD, "\x05\x01\x00")
SOCKS_HANDSHAKE_SERVER = simple_table_translate(SD, "\x05\x00")
SOCKS_REQUEST_OK = simple_table_translate(SD, "\x05\x00\x00\x01%s%s" % (_inet_aton("0.0.0.0"), pack(">H", 8888)))


log_file = open("server.log", "w")

def run_as_user(user):
    try:
        db = pwd.getpwnam(user)
    except KeyError:
        raise Exception("user doesn't exists") 
    try:
        os.setgid(db.pw_gid)
    except OSError:        
        raise Exception("change gid failed") 
    try:
        os.setuid(db.pw_uid)
    except OSError:
        raise Exception("change uid failed") 

def daemonize():
    try:
        status = os.fork()
    except OSError as e:
        print e
    if not status: 
        os.setsid()
        os.close(0)
        os.close(1)
        os.close(2)
        stdin = open("/dev/null", "r")
        os.dup2(log_file.fileno(), 1)
        os.dup2(log_file.fileno(), 2)
        try:
            status2 = os.fork()
        except OSError as e:
            print e
        if status2:
            exit()
    else:
        exit()        


def server_config():
    global sockfd, ep, _accept
    sock = _socket(_AF_INET, _SOCK_STREAM) 
    sock.setsockopt(_SOL_SOCKET, _SO_REUSEADDR, 1)
    sock.bind((SERVER_IP, SERVER_PORT)) 
    sock.listen(MAX_LISTEN) 
    _accept = sock.accept 
    sock.setsockopt(_SOL_SOCKET, _SO_REUSEADDR, 1) 
    sockfd = sock.fileno() 
    ep = epoll()
    ep.register(sockfd, EPOLLIN | EPOLLERR | EPOLLHUP) 


def clean_queue(fd):
    if fd not in cons:
        return
    #close pipe
    context_client = cons[fd]
    server = True
    try:
        server_fd  = context_client["to_conn"].fileno()
    except:
        server = False
    if server:
        context_server = cons[server_fd]
    #close client buffer
    context_client["in_buffer"].close()
    context_client["out_buffer"].close()
    if server:
        #close server buffer
        context_server["in_buffer"].close()
        context_server["out_buffer"].close()
    #close client socket
    from_conn = context_client["from_conn"]
    try:
        from_conn.shutdown(socket.SHUT_RDWR) 
    except:
        pass
    ep.unregister(fd) 
    from_conn.close() 
    if server:
        #close server socket
        from_conn = context_server["from_conn"]
        try: 
            from_conn.shutdown(socket.SHUT_RDWR) 
        except: 
            pass
        ep.unregister(from_conn) 
        from_conn.close() 
        del cons[server_fd]
    #delete context
    del cons[fd] 



STATUS_HANDSHAKE = 0x1 << 1 
STATUS_REQUEST = 0x1 << 2
STATUS_WAIT_REMOTE = 0x1 << 3
STATUS_DATA = 0x1 << 4

STATUS_SERVER_HANDSHKAE = 0x1 << 5
STATUS_SERVER_REQUEST = 0x1 << 6 
STATUS_SERVER_CONNECTED = 0x1 <<7
STATUS_SERVER_WAIT_REMOTE = 0x1 << 8



def handle_data(event, fd):
    #epoll event after clean_queue
    if fd not in cons:
        clean_queue(fd)
        return 
    #lazy unpack context
    context = cons[fd] 
    crypted, status, from_conn, to_conn, in_buffer, active, out_buffer, request = context.values() 
    if to_conn:
        to_context = cons[to_conn.fileno()] 
    #pdb.set_trace() 
    if (event & EPOLLOUT) and out_buffer.tell():
        try: 
            data = out_buffer.getvalue()
            data_count = len(data) 
            data_sent = from_conn.send(data) 
            if data_sent != data_count: 
                out_buffer.truncate(0)
                out_buffer.write(data[data_sent:])
                return
        except socket.error as e: 
            if e.errno == errno.EAGAIN: 
                return
            else: 
                clean_queue(fd)
                return
        out_buffer.truncate(0) 

    if status & STATUS_HANDSHAKE: 
        if event & EPOLLIN: 
            raw = from_conn.recv(128) 
            #maybe RST
            if not raw:
                clean_queue(fd)
                return 
            if not raw.startswith("\x05\x01"): 
                print "weird handshake"
                clean_queue(fd)
                return
            #handshake packet or not 
            if len(raw) != 3: 
                clean_queue(fd)
                return
            #connect our server
            try:        
                request_sock = _socket(_AF_INET, _SOCK_STREAM)
                request_sock.setblocking(0)  
                request_fd = request_sock.fileno()
                ep.register(request_fd, EPOLLIN|EPOLLOUT) 
            except Exception as e: 
                clean_queue(fd)
                return 
            #request context 
            cons[request_fd] = {
                    "in_buffer": StringIO(),
                    "out_buffer": StringIO(),
                    "from_conn": request_sock,
                    "to_conn": from_conn,
                    "crypted": False, 
                    "request": "",
                    "status": STATUS_SERVER_CONNECTED,
                    "active": time()
                    } 
            context["to_conn"] = request_sock
            #next status , CONNECTED
            context["status"] = 0
            context["request"] = ""
            try: 
                request_sock.connect(REMOTE)
            except socket.error as e: 
                #close connection if it's a real exception
                if e.errno != errno.EINPROGRESS:
                    clean_queue(fd) 
                    return 
            return

    if event & EPOLLIN: 
        try:
            text = from_conn.recv(256) 
        except socket.error:
            clean_queue(fd)
            return
        #pdb.set_trace()
        #may RST
        if not text:
            clean_queue(fd)
            return 
        raw = text
        #if this msg if from server, decrypt it
        if not crypted: 
            raw = simple_table_translate(DS, text)            
        #pdb.set_trace()
        if raw == "\x05\x00":
            status = STATUS_SERVER_HANDSHKAE 
        elif raw.startswith("\x05\x01\x00"):
            status = STATUS_REQUEST 
        elif raw.startswith("\x05\x00\x00\x01"): 
            status = STATUS_SERVER_WAIT_REMOTE 
        else:            
            status = STATUS_DATA 

    if status & STATUS_SERVER_CONNECTED:
        #ok we have connected our server 
        #send it HANDSHAKE 
        if event & EPOLLOUT:
            try: 
                from_conn.sendall(SOCKS_HANDSHAKE_CLIENT) 
            except socket.error: 
                out_buffer.write(SOCKS_HANDSHAKE_CLIENT) 
                return  
            context["status"] = STATUS_SERVER_HANDSHKAE 
            return 

    if status & STATUS_SERVER_HANDSHKAE:
        #we received HANDSHAKE from SERVER
        #send OK to client
        if not (event & (~EPOLLOUT)):
            return 
        try:
            to_conn.sendall("\x05\x00")
        except socket.error:
            to_context["out_buffer"].write(SOCKS_HANDSHAKE_CLIENT)
            return
        #client may REQUEST 
        to_context["status"] = STATUS_REQUEST
        return

    if status & STATUS_REQUEST: 
        if not (event & (~EPOLLOUT)):
            return 
        #for local information only
        parse_buffer = StringIO()
        parse_buffer.write(text)
        parse_buffer.seek(4) 
        addr_to = text[3]
        addr_type = ord(addr_to)
        if addr_type == 1:
            addr = parse_buffer.read(4)
            addr_to += addr
        elif addr_type == 3: 
            addr_len = parse_buffer.read(1)
            addr = parse_buffer.read(ord(addr_len))
            addr_to += addr_len + addr
        elif addr_type == 4:
            addr = parse_buffer.read(16)
            net = _inet_ntop(socket.AF_INET6, addr)
            addr_to += net
        addr_port = parse_buffer.read(2) 
        parse_buffer.close()
        addr_to += addr_port
        #maybe wrong status
        to_data =False
        try:
            port = unpack(">H", addr_port)
        except struct.error: 
            to_data = True 
        #change status to DATA if this packet is not a REQUEST
        if not to_data: 
            try:        
                to_conn.sendall(simple_table_translate(SD, text)) 
            except socket.error as e: 
                if e.errno == errno.EAGAIN:
                    to_context["out_buffer"].write(simple_table_translate(SD, text))  
                return 
            remote = (addr, port[0])
            print "new request", remote
            context["request"] = remote
            to_context["request"] = remote
        else: 
            status = STATUS_DATA 

    if status & STATUS_SERVER_WAIT_REMOTE:
        #SERVER ok,  send request OK to client 
        if not (event & EPOLLOUT):
            return 
        #pdb.set_trace()
        msg = "\x05\x00\x00\x01%s%s" % (_inet_aton("0.0.0.0"),
                pack(">H", 8888)) 
        try: 
            to_conn.sendall(msg)
        except socket.error:
            to_context["out_buffer"].write(msg)
            return 
        #next,  DATA
        context["status"] = STATUS_DATA
        to_context["status"] = STATUS_DATA 

    if status & STATUS_DATA: 
        if event & EPOLLIN: 
            to_out_buffer = to_context["out_buffer"] 
            #write data to buffer, buffer size 1M
            #we don't read more until we can send them out
            if to_out_buffer.tell():
                if to_out_buffer.tell() > 0x800000: 
                    return 
                if not crypted:
                    raw = simple_table_translate(SD, text)
                else:
                    raw = simple_table_translate(DS, text) 
                to_out_buffer.write(raw) 
                return
            in_buffer.write(text)  
            try: 
                data = from_conn.recv(4096)
                in_buffer.write(data) 
                data_count = in_buffer.tell() 
                if crypted:
                    raw = simple_table_translate(SD,
                            in_buffer.getvalue())
                else:
                    raw = simple_table_translate(DS,
                            in_buffer.getvalue())
                data_sent = to_conn.send(raw) 
                if data_sent != data_count:
                    in_buffer.seek(data_sent)
                    to_out_buffer.write(in_buffer.read()) 
            except socket.error as e:
                if e.errno == errno.EAGAIN: 
                    if crypted:
                        raw = simple_table_translate(SD,
                                in_buffer.getvalue()) 
                    else:
                        raw = simple_table_translate(DS,
                                in_buffer.getvalue())
                    to_out_buffer.write(raw) 
                else:
                    clean_queue(fd) 
                    return
            in_buffer.truncate(0)
            return 

def handle_connection():
    conn, addr = _accept() 
    fd = conn.fileno() 
    conn.setblocking(0)
    ep.register(fd, EPOLLIN|EPOLLOUT)
    #add fd to queue
    cons[fd] = {
            "in_buffer": StringIO(),
            "out_buffer": StringIO(),
            "from_conn": conn,
            "to_conn": None,
            "crypted": True,
            "request": None,
            "status": STATUS_HANDSHAKE,
            "active":time()
            } 

def poll_wait(): 
    #if not has_in_event, make loop 10000 times slower
    has_in_event = True
    ep_poll = ep.poll
    while True: 
        if has_in_event:
            sleep_time = 0.000001
            has_in_event = False
        else:
            sleep_time = 0.1 
        sleep(sleep_time) 
        for fd, event in ep_poll(): 
            if event & EPOLLIN:
                has_in_event = True
            if fd == sockfd:
                if event & EPOLLIN:
                    handle_connection()
                else:
                    raise Exception("main socket error")
            else:
                handle_data(event, fd) 

if __name__ == "__main__":
    server_config() 
    daemonize()
    poll_wait()