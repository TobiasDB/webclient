from webclient import WebClient


with WebClient() as wc:
    doc = wc.fetch("https://books.toscrape.com/", browser="auto")
    
    print(doc.flags())