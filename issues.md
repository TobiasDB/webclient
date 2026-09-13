Lets have an event backing to consolodate a lot of methods currently on the Document

xhr_requests,
dom_mutations
console
subscribe
action_events
events_of -> Events of should be on the stub cleanly

Also ref, text, join should be on the DocumentCore and exposed on the stub

_core, _capabilities, _is_empty all can be removed after this is done, Document is now just a stub.


From reference we can remove __new__, construction can be handled externally, it also have some type trickery with lazy - is this stale?

from_url can be moved off, and bound is really a implementation detail thing


The webbase can lose _aextract and _extracted and fields -> these function do not belond on our stub classes
As with Document, _capabilities, is_empty, __repr_args can all move off. Now it is is just a reuse-able stub 


I also dont see why _aextract_all and _afilter or _is_empty have to live on the collectio.


We also still have when, then, otherwise attached to field, these should clearly be on some expression builder like poalrs e.g. pl.when().then().otherwise()

Once all of these models are cleaned up they can join the Lazy models in stubs.py and this can be renamed interface