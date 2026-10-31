"""Project HTTP transport: never inherit environment or Windows system proxies."""
import urllib.request


def direct_opener(*handlers):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)


def urlopen(request, data=None, timeout=30):
    return direct_opener().open(request, data=data, timeout=timeout)
