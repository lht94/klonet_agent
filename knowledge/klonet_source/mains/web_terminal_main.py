from vemu_uestc.webserver.app_factory import create_web_terminal_app

if __name__ == "__main__":
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    app = create_web_terminal_app()
    server = pywsgi.WSGIServer(
            ('0.0.0.0', 5005), app, handler_class=WebSocketHandler)
    print("Started!")
    server.serve_forever()
