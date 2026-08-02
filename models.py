from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Float
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime

Base = declarative_base()

class Patient(Base):
    __tablename__ = "patients"
    
    id = Column(String, primary_key=True, index=True) # MRN or similar
    name = Column(String, index=True)
    dob = Column(String) # Keeping simple for now
    gender = Column(String)
    
    studies = relationship("Study", back_populates="patient")

class Study(Base):
    __tablename__ = "studies"
    
    study_uid = Column(String, primary_key=True, index=True)
    patient_id = Column(String, ForeignKey("patients.id"))
    modality = Column(String) # CT, MR, XR
    study_date = Column(DateTime, default=datetime.utcnow)
    description = Column(String)
    
    # AI Analysis Results
    critical_finding = Column(Boolean, default=False)
    ai_confidence = Column(Float, default=0.0)
    ai_summary = Column(String, nullable=True)
    
    patient = relationship("Patient", back_populates="studies")
