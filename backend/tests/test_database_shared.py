"""
Test that database is shared across separate engine instances (B3).

Per QA: Worker and API must resolve to the same database file.
"""

from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import tempfile
from pathlib import Path

from backend.config import Settings
from backend.database import ReviewCardDB, Base


def test_two_engines_see_same_row():
    """
    Test that two separate engine instances see the same row (B3).
    
    This verifies that the database URL resolves to an absolute path,
    so Worker and API processes can share the same database.
    """
    # Create a temp database file
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    
    try:
        # Settings with absolute path
        settings = Settings(
            database_url=f"sqlite:///{db_path.absolute()}"
        )
        
        with patch("backend.config.get_settings", return_value=settings), \
             patch("backend.database.get_settings", return_value=settings):
            
            # Clear any cached engines
            import backend.database
            backend.database._engine = None
            backend.database._SessionLocal = None
            
            # First engine/session: Write a row
            engine1 = create_engine(
                settings.get_resolved_database_url(),
                connect_args={"check_same_thread": False}
            )
            Base.metadata.create_all(engine1)
            Session1 = sessionmaker(bind=engine1)
            session1 = Session1()
            
            card = ReviewCardDB(
                workflow_id="test:shared:001",
                post_data={"id": "123", "text": "test"},
                evidence_data={"post_id": "123"},
                status="research",
                created_at="2026-11-21T08:00:00Z",
                updated_at="2026-11-21T08:00:00Z",
            )
            session1.add(card)
            session1.commit()
            session1.close()
            engine1.dispose()
            
            # Second engine/session: Read the same row
            engine2 = create_engine(
                settings.get_resolved_database_url(),
                connect_args={"check_same_thread": False}
            )
            Session2 = sessionmaker(bind=engine2)
            session2 = Session2()
            
            retrieved = session2.query(ReviewCardDB).filter(
                ReviewCardDB.workflow_id == "test:shared:001"
            ).first()
            
            assert retrieved is not None, "Second engine should see row from first engine"
            assert retrieved.workflow_id == "test:shared:001"
            assert retrieved.post_data["id"] == "123"
            
            session2.close()
            engine2.dispose()
            
    finally:
        # Cleanup
        db_path.unlink(missing_ok=True)
