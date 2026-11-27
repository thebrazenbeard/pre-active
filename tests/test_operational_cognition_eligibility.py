from pathlib import Path
import hashlib
import pytest
from pre_active.store import Store
from pre_active.volition_bridge import VolitionBridge
from pre_active.engine import Engine, ModelResponse, ToolCall
from pre_active.context import ContextAssembler
from pre_active.tools import ToolRegistry
from volition import DriveKind, ProvenanceClass, GoalCompletionClaim, GoalCompletionVerifier
from volition.operational_drive import ObservationClaim, ObservationVerifier, ModelWantProposal, OperationalMotivation

def submit(s, ident, magnitude=.9, now=1., target="count"):
    c=ObservationClaim(evidence_id=ident,content_digest="sha256:"+hashlib.sha256(ident.encode()).hexdigest(),target=target,kind=DriveKind.OPEN_LOOP,magnitude=magnitude,confidence=1.,source_ref="fixture:"+ident,provenance=ProvenanceClass.CURRENT_OBSERVATION)
    return VolitionBridge(s).submit_model_want_proposal(proposal=ModelWantProposal(proposal_id="proposal-"+ident,target=target,kind=c.kind,evidence_id=ident,reason="fixture"),admitted_observation=ObservationVerifier([c],verifier_id="fixture").admit(c),now=now)

def goal(s):
    return OperationalMotivation.from_snapshot(s.get_operational_motivation_state()["snapshot"]).engine.active_goal

def complete(s, now=2.):
    g=goal(s)
    c=GoalCompletionClaim(outcome_event_id="outcome-1",goal_id=g.goal_id,choice_id=g.choice_id,goal_revision=g.revision,target=g.target,result_receipt_id="fixture-result",evidence_ref="fixture-evidence",source_ref="fixture-evaluator",provenance=ProvenanceClass.CURRENT_OBSERVATION)
    return VolitionBridge(s).record_operational_goal_completion(admitted=GoalCompletionVerifier([c],verifier_id="fixture").admit(c),now=now)

class Model:
    def __init__(self, callback=None, reenter=False):
        self.calls=0
        self.callback=callback
        self.reenter=reenter
    def respond(self, *,messages,tools):
        self.calls+=1
        if self.callback:self.callback()
        if self.reenter and self.calls==1:
            return ModelResponse(tool_call=ToolCall("turn-1","pre_active.request_turn",{"reason":"one more observation","delay_seconds":0.}))
        return ModelResponse(final_text="fixture answer")

def engine(s,m):
    return Engine(store=s,model=m,tools=ToolRegistry(s),context=ContextAssembler(s),system_prompt="Fixture",worker_id="fixture",max_steps=4,max_autonomous_turns_per_run=1,max_consecutive_endogenous_turns=1)

def event(s,id):
    return next(x for x in s.list_events() if x["id"]==id)

@pytest.mark.parametrize("change",["suspend","complete","revise","expire"])
def test_stale_pending_opportunity_retires_before_grant(tmp_path,change):
    s=Store(tmp_path/"state.db");r=submit(s,"first")
    if change=="suspend":submit(s,"low",0.,2.)
    if change=="complete":complete(s)
    if change=="revise":submit(s,"revision",.8,2.)
    m=Model();e=engine(s,m)
    # The revised goal's new opportunity may be granted; old event may not.
    e.run_once(now=21602. if change=="expire" else 3.)
    assert event(s,r["cognition_event_id"])["status"] in {"DONE","CANCELLED"}
    assert not s.connection.execute("SELECT id FROM runs WHERE source_event_id=?",(r["cognition_event_id"],)).fetchone()
    assert any(j["event_type"]=="OPERATIONAL_COGNITION_RETIRED" and j["subject_id"]==r["cognition_event_id"] for j in s.list_journal())
    assert m.calls==0;s.close()

def test_current_positive_and_unrelated_revision(tmp_path):
    s=Store(tmp_path/"state.db");r=submit(s,"first");g=goal(s)
    other=submit(s,"other",.4,2.,target="other")
    assert other["cognition_event_id"] is None and goal(s)==g
    m=Model();e=engine(s,m);rid=e.run_once(now=3.)
    assert rid and e.run_once(now=4.)==rid
    assert s.get_run(rid)["status"]=="COMPLETED" and m.calls==1
    assert s.get_run(rid)["capabilities"]==set();s.close()

@pytest.mark.parametrize("where",["before-model","in-flight","reentry"])
def test_stale_granted_run_cannot_publish_or_reenter(tmp_path,where):
    path=tmp_path/"state.db";s=Store(path);r=submit(s,"first")
    def withdraw():
        other=Store(path);submit(other,"withdraw",0.,4.);other.close()
    m=Model(callback=withdraw if where=="in-flight" else None,reenter=where=="reentry")
    e=engine(s,m);rid=e.run_once(now=2.);assert rid
    if where=="before-model":withdraw()
    assert e.run_once(now=5.)==rid
    if where=="reentry":
        assert s.get_run(rid)["status"]=="WAITING";withdraw()
        assert e.run_once(now=6.)==rid
    run=s.get_run(rid)
    assert run["status"]=="CANCELLED" and run["final_text"] is None
    assert m.calls==(0 if where=="before-model" else 1)
    assert run["capabilities"]==set()
    assert not any(x["status"]=="PENDING" for x in s.list_events(kind="run.step"))
    s.close()

def test_binding_and_duplicate_recovery(tmp_path):
    path=tmp_path/"state.db";s=Store(path);r=submit(s,"first");g=goal(s)
    v=event(s,r["cognition_event_id"])["payload"]["volition"]
    assert v["choice_id"]==g.choice_id and v["goal_revision"]==g.revision
    assert v["evidence_id"]=="first" and v["admitted_at"]==1.
    assert v["expires_at"]==21601.
    s.close();s=Store(path);rr=submit(s,"first",now=2.)
    assert not rr["new_evidence"] and rr["cognition_event_id"]==r["cognition_event_id"]
    assert len(s.list_events(kind="autonomous.turn"))==1;s.close()

def test_legacy_turn_unchanged(tmp_path):
    s=Store(tmp_path/"state.db");s.enqueue_event(kind="autonomous.turn",payload={"task":"legacy","capabilities":[],"source":"EXTERNAL","reason":"fixture"},now=1.)
    m=Model();e=engine(s,m);rid=e.run_once(now=2.)
    assert e.run_once(now=3.)==rid and s.get_run(rid)["status"]=="COMPLETED" and m.calls==1
    s.close()

def test_positive_bounded_reentry_keeps_origin(tmp_path):
    s=Store(tmp_path/"state.db");r=submit(s,"first");m=Model(reenter=True);e=engine(s,m)
    rid=e.run_once(now=2.);assert e.run_once(now=3.)==rid
    assert s.get_run(rid)["status"]=="WAITING"
    assert e.run_once(now=4.)==rid
    assert s.get_run(rid)["status"]=="COMPLETED" and m.calls==2
    assert s.run_source_event_id(rid)==r["cognition_event_id"]
    assert s.get_run(rid)["capabilities"]==set();s.close()

def test_legacy_unbound_operational_event_fails_closed(tmp_path):
    import json
    s=Store(tmp_path/"state.db");r=submit(s,"first");id=r["cognition_event_id"]
    payload=event(s,id)["payload"]
    for key in ("choice_id","goal_revision","admitted_at","expires_at"):
        payload["volition"].pop(key)
    s.connection.execute("UPDATE events SET payload_json=? WHERE id=?",(json.dumps(payload),id))
    m=Model();assert engine(s,m).run_once(now=2.) is None
    assert event(s,id)["status"]=="DONE" and m.calls==0
    assert not s.connection.execute("SELECT id FROM runs WHERE source_event_id=?",(id,)).fetchone()
    assert any(j["event_type"]=="OPERATIONAL_COGNITION_RETIRED" for j in s.list_journal())
    s.close()

def test_grant_rollback_then_recover_exactly_once(tmp_path,monkeypatch):
    path=tmp_path/"state.db";s=Store(path);r=submit(s,"first");id=r["cognition_event_id"]
    m=Model();e=engine(s,m)
    original=s.ack_event
    def interrupted(event_id,**kw):
        if event_id==id:raise RuntimeError("synthetic grant transaction interruption")
        return original(event_id,**kw)
    monkeypatch.setattr(s,"ack_event",interrupted)
    with pytest.raises(RuntimeError,match="synthetic grant"):
        e.run_once(now=2.)
    assert not s.connection.execute("SELECT id FROM runs WHERE source_event_id=?",(id,)).fetchone()
    assert not s.list_events(kind="run.step")
    s.close();s=Store(path);e=engine(s,m)
    rid=e.run_once(now=100.)
    assert rid and event(s,id)["status"]=="DONE"
    assert e.run_once(now=101.)==rid
    assert s.get_run(rid)["status"]=="COMPLETED" and m.calls==1
    assert len(s.connection.execute("SELECT id FROM runs WHERE source_event_id=?",(id,)).fetchall())==1
    s.close()

def test_withdrawal_during_context_construction_denies_model(tmp_path,monkeypatch):
    path=tmp_path/"state.db";s=Store(path);submit(s,"first")
    m=Model();e=engine(s,m);rid=e.run_once(now=2.)
    original=e._messages_for_run
    def messages(*a,**kw):
        out=original(*a,**kw)
        other=Store(path);submit(other,"withdraw",0.,3.);other.close()
        return out
    monkeypatch.setattr(e,"_messages_for_run",messages)
    assert e.run_once(now=4.)==rid
    assert s.get_run(rid)["status"]=="CANCELLED" and m.calls==0
    s.close()

def test_expiry_after_grant_before_model(tmp_path):
    s=Store(tmp_path/"state.db");submit(s,"first");m=Model();e=engine(s,m)
    rid=e.run_once(now=2.);assert e.run_once(now=21602.)==rid
    assert s.get_run(rid)["status"]=="CANCELLED" and m.calls==0;s.close()

def test_stored_model_decision_cannot_publish_after_withdrawal(tmp_path,monkeypatch):
    path=tmp_path/"state.db";s=Store(path);submit(s,"first")
    m=Model();e=engine(s,m);rid=e.run_once(now=2.)
    def interrupt(**kwargs):
        raise RuntimeError("synthetic publication interruption")
    monkeypatch.setattr(s,"complete_run_step",interrupt)
    with pytest.raises(RuntimeError,match="synthetic publication"):
        e.run_once(now=3.)
    assert s.get_run_step_decision(run_id=rid,step=0) is not None
    assert s.get_run(rid)["final_text"] is None and m.calls==1
    s.close();s=Store(path);submit(s,"withdraw",0.,4.)
    e=engine(s,m);assert e.run_once(now=100.)==rid
    assert s.get_run(rid)["status"]=="CANCELLED" and s.get_run(rid)["final_text"] is None
    assert m.calls==1;s.close()

def admitted_completion(s, ident="outcome-2"):
    g=goal(s)
    c=GoalCompletionClaim(outcome_event_id=ident,goal_id=g.goal_id,choice_id=g.choice_id,goal_revision=g.revision,target=g.target,result_receipt_id="fixture-result",evidence_ref="fixture-evidence",source_ref="fixture-evaluator",provenance=ProvenanceClass.CURRENT_OBSERVATION)
    return GoalCompletionVerifier([c],verifier_id="fixture").admit(c)

@pytest.mark.parametrize("when",[21601.,21602.])
def test_expired_completion_never_credits_goal(tmp_path,when):
    s=Store(tmp_path/"state.db");submit(s,"first");a=admitted_completion(s)
    with pytest.raises(ValueError,match="exact active goal|expired"):
        VolitionBridge(s).record_operational_goal_completion(admitted=a,now=when)
    assert not any(j["event_type"]=="OPERATIONAL_GOAL_COMPLETION_ADMITTED" for j in s.list_journal())
    state=s.get_operational_motivation_state()
    assert state["updated_at"]==when
    assert not any(e.kind=="GOAL_COMPLETED" for e in OperationalMotivation.from_snapshot(state["snapshot"]).engine.events)
    s.close()

def test_historical_completion_replay_does_not_advance_new_goal(tmp_path):
    s=Store(tmp_path/"state.db");submit(s,"first");a=admitted_completion(s)
    b=VolitionBridge(s);assert b.record_operational_goal_completion(admitted=a,now=2.)["newly_completed"]
    submit(s,"second",now=3.);before=s.get_operational_motivation_state()
    assert not b.record_operational_goal_completion(admitted=a,now=99999.)["newly_completed"]
    assert s.get_operational_motivation_state()==before
    s.close()

@pytest.mark.parametrize("horizon",[0.,-1.])
def test_nonpositive_horizon_appraises_without_queued_dispatch(tmp_path,horizon):
    from volition import VolitionEngine,Policy
    s=Store(tmp_path/"state.db")
    s.save_operational_motivation_state(OperationalMotivation(VolitionEngine(Policy(goal_reappraisal_seconds=horizon))).snapshot(),expected_revision=0,now=1.)
    r=submit(s,"first",now=1.)
    assert r["goal_id"] and goal(s) is not None
    assert r["cognition_event_id"] is None
    assert not s.list_events(kind="autonomous.turn")
    assert OperationalMotivation.from_snapshot(s.get_operational_motivation_state()["snapshot"]).engine.snapshot()["endogenous_turns"]==0
    m=Model();assert engine(s,m).run_once(now=2.) is None
    assert m.calls==0
    s.close()
