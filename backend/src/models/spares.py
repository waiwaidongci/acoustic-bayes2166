from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey,UniqueConstraint
from ..config.database import Base

class SparesStudy(Base):
    """一次已完成的分类研究对应一份备件反演研究；判读件数更正不会改写历史版本。"""
    __tablename__='spares_studies'
    __table_args__=(UniqueConstraint('run_id',name='uq_spares_study_run'),)
    id=Column(Integer,primary_key=True)
    run_id=Column(Integer,ForeignKey('classifier_runs.id'),nullable=False)
    name=Column(String(160),nullable=False)
    created_by=Column(String(120),nullable=False)
    created_at=Column(String(40),nullable=False)

class SparesVersion(Base):
    """冻结资料：折外混淆矩阵、判读件数、允许偏差与目标故障在创建版本时固定。"""
    __tablename__='spares_versions'
    __table_args__=(UniqueConstraint('study_id','version_no',name='uq_spares_version_no'),)
    id=Column(Integer,primary_key=True)
    study_id=Column(Integer,ForeignKey('spares_studies.id'),nullable=False)
    version_no=Column(Integer,nullable=False)
    target_label=Column(String(120),nullable=False)
    tolerance=Column(String(20),nullable=False)
    counts=Column(JSON,nullable=False)
    matrix=Column(JSON,nullable=False)
    labels=Column(JSON,nullable=False)
    checksum=Column(String(64),nullable=False)
    state=Column(String(20),default='computing')
    created_by=Column(String(120),nullable=False)
    created_at=Column(String(40),nullable=False)
    published_at=Column(String(40),default='')

class SparesEnd(Base):
    """每个版本的两个极值各自一行；同版两端齐备才发布该版本。"""
    __tablename__='spares_ends'
    __table_args__=(UniqueConstraint('version_id','end',name='uq_spares_end'),)
    id=Column(Integer,primary_key=True)
    version_id=Column(Integer,ForeignKey('spares_versions.id'),nullable=False)
    end=Column(String(3),nullable=False)
    state=Column(String(20),default='queued')
    result=Column(JSON,nullable=True)
    reason=Column(Text,default='')
    attempts=Column(Integer,default=0)
    claimed_at=Column(String(40),default='')
    enqueued_at=Column(String(40),default='')
    enqueued_at=Column(String(40),default='')
    finished_at=Column(String(40),default='')
    created_at=Column(String(40),nullable=False)
