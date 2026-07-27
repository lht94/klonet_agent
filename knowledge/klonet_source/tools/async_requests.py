from functools import partial
import traceback

import gevent
from gevent.pool import Pool
from requests import Session


class AsyncRequest(object):

    def __init__(self, method, url, **kwargs):
        self.method = method
        self.url = url
        self.session = kwargs.pop('session', None)
        if self.session is None:
            self.session = Session()
            self._close = True
        else:
            self._close = False

        callback = kwargs.pop('callback', None)
        if callback:
            kwargs['hooks'] = {'response': callback}
        self.kwargs = kwargs
        self.response = None

    def send(self, **kwargs):
        merged_kwargs = {}
        merged_kwargs.update(self.kwargs)
        merged_kwargs.update(kwargs)
        try:
            self.response = self.session.request(
                self.method, self.url, **merged_kwargs
            )
        except Exception as e:
            self.exception = e
            self.traceback = traceback.format_exc()
        finally:
            if self._close:
                self.session.close()
        return self


def send(r, pool=None, stream=False):
    if pool is not None:
        return pool.spawn(r.send, stream=stream)
    return gevent.spawn(r.send, stream=stream)


get = partial(AsyncRequest, 'GET')
options = partial(AsyncRequest, 'OPTIONS')
head = partial(AsyncRequest, 'HEAD')
post = partial(AsyncRequest, 'POST')
put = partial(AsyncRequest, 'PUT')
patch = partial(AsyncRequest, 'PATCH')
delete = partial(AsyncRequest, 'DELETE')


def request(method, url, **kwargs):
    return AsyncRequest(method, url, **kwargs)


def map(requests, stream=False, size=None, exception_handler=None, gtimeout=None):
    requests = list(requests)
    print(requests)
    pool = Pool(size) if size else None
    jobs = [send(r, pool, stream=stream) for r in requests]
    print('creating request jobs...')
    gevent.joinall(jobs, timeout=gtimeout)
    print('job done...')
    print(f'requests results are {requests}')
    ret = []
    for request in requests:
        print(f'getting async response result: {request}  ...')
        print(f'ret is {ret}')

        if request.response is not None:
            ret.append(request.response)
        elif exception_handler and hasattr(request, 'exception'):
            ret.append(exception_handler(request, request.exception))
        elif exception_handler and not hasattr(request, 'exception'):
            ret.append(exception_handler(request, None))
        else:
            ret.append(None)
    print(ret)
    return ret


def imap(requests, stream=False, size=2, exception_handler=None):
    pool = Pool(size)

    def send(r):
        return r.send(stream=stream)
    
    for request in pool.imap_unordered(send, requests):
        if request.response is not None:
            yield request.response
        elif exception_handler:
            ex_result = exception_handler(request, request.exception)
            if ex_result is not None:
                yield ex_result
    pool.join()
    