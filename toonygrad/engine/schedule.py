from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from toonygrad.helpers import Context
from toonygrad.ops import UOp, graph_rewrite, PatternMatcher, UPat, UOps, symbolic, track_rewrites, buffers
from toonygrad.engine.lazy import LazyBuffer
from toonygrad.shape.symbolic import Variable
from toonygrad.shape.shapetracker import ShapeTracker
from toonygrad.device import Buffer

@dataclass(frozen=True)
class ScheduleItem:
  ast: UOp
  bufs: Tuple[Buffer, ...]

pm_merge_views = PatternMatcher([
  # merge VIEW
  (UPat(UOps.VIEW, src=(UPat(UOps.VIEW, name="s0"),), name="s1"),
   lambda s0,s1: UOp(UOps.VIEW, s1.dtype, s0.src, s0.arg+s1.arg)),
  # const + copy = const
  (UPat(UOps.COPY, src=(UPat.cvar('c'),)), lambda c: c),
  # const + maskless swizzle = const
  (UPat(UOps.VIEW, src=(UPat.cvar('c'),), name="s"),
    lambda s,c: c if all(x.mask is None for x in s.st.views) else None),
])

pm_all = symbolic+pm_merge_views+PatternMatcher([
  # VIEW before ALU
  (UPat(UOps.VIEW, src=(UPat(UOps.ALU, name="alu"),), name="s"),
    lambda alu,s: UOp(UOps.ALU, alu.dtype,
                      tuple(UOp(UOps.VIEW, x.dtype, (x,), s.arg) for x in alu.src), alu.arg)),
])

def create_buffer(ctx:Dict[UOp, UOp], store_me:UOp, load_me:Optional[UOp]=None):
  if (stored:=ctx.get(store_me)) is None:
    buffer = UOp.new_buffer(store_me.dtype, store_me.device, store_me.size)
    stored = ctx[store_me] = UOp.store(buffer, ShapeTracker.from_shape(store_me.shape).to_uop(), store_me)
  else:
    if load_me is None: return None
  return UOp.load(stored.src[0],
                  load_me.st.to_uop() if load_me is not None and load_me.op is UOps.VIEW else ShapeTracker.from_shape(store_me.shape).to_uop(),
                  stored, dtype=store_me.dtype)

create_buffers = PatternMatcher([
  (UPat(UOps.VIEW, src=(UPat(UOps.BUFFER, name='store_me'),), name="load_me"),
   lambda ctx, store_me, load_me: UOp.load(store_me, load_me.st.to_uop(), dtype=load_me.dtype)),
  (UPat((UOps.VIEW, UOps.CONTIGUOUS), src=(UPat.var('store_me'),), name="load_me"), create_buffer),
  (UPat(UOps.COPY, name="store_me"), create_buffer),
  (UPat(UOps.SINK, name="sink"),
   lambda ctx,sink:
    UOp.sink(*[create_buffer(ctx,x) if x.op not in (UOps.STORE, UOps.LOAD) else (x.src[-1] if x.op is UOps.LOAD else x) for x in sink.src])
    if all(x.op is not UOps.STORE for x in sink.src) else None),
])

def append_kernel(k:List[UOp], base:UOp): k.append(base.sink())
break_sched = PatternMatcher([
  (UPat(UOps.STORE, name="base"), append_kernel),
  (UPat(UOps.LOAD, src=(UPat(), UPat(), UPat()), name="ld"), lambda k,ld: UOp.load(ld.src[0], ld.src[1], dtype=ld.dtype)),
])

def append_buffer(b:List[Buffer], base:UOp):
  if base.buffer not in b: b.append(base.buffer)
  # should this be the ptr, or the buffer?
  return UOp(UOps.DEFINE_GLOBAL, base.dtype.ptr(), (), b.index(base.buffer))
enumerate_bufs = PatternMatcher([
  (UPat(UOps.BUFFER, name="base"), append_buffer),
  # copy is just copy
  (UPat.sink(UPat.store(UPat(name="dest"), UPat(UOps.VIEW, name="st"),
                        UPat(UOps.COPY, src=(UPat.load(UPat(name="src"), UPat(UOps.VIEW, name="st")),), name="cpy"))),
                        lambda _, dest, src, st, cpy: UOp(UOps.COPY, dest.dtype, (dest, src), cpy.arg) if st.st.contiguous else None),
])

# ****

pm_remove_buffer = PatternMatcher([(UPat(UOps.BUFFER, src=(UPat.var('x'),)), lambda x: x), ])
def add_buffer(to_realize:Tuple[Dict[UOp, bool], Dict[UOp, UOp]], x:UOp):
  #print(x.op, x.arg)
  # TODO: ugh, this is the worst way to do this
  with Context(TRACK_MATCH_STATS=0): x_bl = graph_rewrite(x, pm_remove_buffer)
  if to_realize.get(x_bl) is False:
    #print(len(to_realize), "HIT", sum(to_realize.values()))
    to_realize[x_bl] = True
    return UOp.new_buffer(x.dtype, x.device, x.size, (x,))
  return None
pm_add_buffer = PatternMatcher([(UPat(tuple(UOps), name="x"), add_buffer), ])

@track_rewrites
def _schedule_rewrite(sink:UOp) -> List[ScheduleItem]:
  sink = graph_rewrite(sink, pm_merge_views)
  """
  sink = graph_rewrite(sink, create_buffers, {})
  graph_rewrite(sink, break_sched, sched:=[])
  ret = []
  for s in sched:
    ast = graph_rewrite(s, enumerate_bufs, bufs:=[])
    ret.append(ScheduleItem(ast, bufs))
  """
  to_realize: Dict[UOp, bool] = {x.base:False for x in sink.src}
  sink = graph_rewrite(sink, pm_merge_views)
  # mark
  for p in sink.sparents:
    if p.op is UOps.COPY:
      to_realize[p.src[0]] = False
      to_realize[p] = False
    if p.op is UOps.CONTIGUOUS:
      to_realize[p] = False
  sink = graph_rewrite(sink, pm_add_buffer, to_realize)
  #replace = {}
  #sink = graph_rewrite(sink, pm_add_buffer, (to_realize, replace), replace)
  #for k in to_realize:
  #  if k in replace:
  #    to_realize[replace[k]] = False
  #sink = graph_rewrite(sink, pm_add_buffer, (to_realize, replace), replace)
  graph_rewrite(sink, PatternMatcher([]))
  ret = []
  return ret

def create_schedule_with_vars(sched:List[UOp]) -> Tuple[List[ScheduleItem], Dict[Variable, int]]:
  sink = UOp.sink(*[x.base for x in sched])
  """
  to_realize: Dict[UOp, None] = {x.base:None for x in sched}
  for p in UOp.sink(*sched).sparents:
    if p.op is UOps.COPY:
      to_realize[p.src[0]] = None
      to_realize[p] = None
    if p.op is UOps.CONTIGUOUS:
      to_realize[p] = None
  """
  sched = _schedule_rewrite(sink)
  print(len(sched))
  return sched, {}
