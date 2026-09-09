

ref = ...
doc = ...

query = (
    ref(...)
    .resolve(...)
    .then(
        doc.select(...).alias("group"),
        rows=doc.select_all(...).map(
            doc.select(...).attr(...).alias("title"),
            price=doc.select(...).attr(...),
            link=doc.select(...).attr(...),
            document=doc.field("link").resolve(...).then(
                content=doc.select('main'),
                links=doc.links(),
            ).otherwise(
                status=doc.status_code,
                message=doc.message,
            )
        )
    ).otherwise(doc.RAISE_ERROR)
)