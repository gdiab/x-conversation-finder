"""Application settings."""

from functools import lru_cache
from typing import Optional
from pathlib import Path
import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration."""
    
    # X API
    x_api_key: str = ""
    x_api_secret: str = ""
    x_bearer_token: str = ""
    
    # X API pricing
    x_post_read_cost: float = 0.005
    x_user_read_cost: float = 0.010
    x_post_create_cost: float = 0.015
    
    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    
    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    
    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    
    # Budget limits
    daily_spend_limit_usd: float = 5.0
    daily_inference_limit_usd: float = 10.0
    budget_kill_switch: bool = False
    
    # Inference pricing (per-token, NB3)
    inference_input_token_cost_usd: float = 0.00000015
    inference_output_token_cost_usd: float = 0.0000006
    
    # Database (B3: absolute path so worker and API resolve to same file)
    database_url: str = ""
    
    def get_resolved_database_url(self) -> str:
        """
        Resolve database_url to an absolute path.
        
        Per B3: Both worker and API must resolve to the same file.
        Default: sqlite:////<repo>/data/x_conversation_finder.db
        """
        if not self.database_url:
            # Default: data/ folder in repo root
            repo_root = Path(__file__).parent.parent.parent
            data_dir = repo_root / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = data_dir / "x_conversation_finder.db"
            return f"sqlite:///{db_path.absolute()}"
        
        # If already starts with sqlite:///, parse and resolve
        if self.database_url.startswith("sqlite:///"):
            path_part = self.database_url[len("sqlite:///"):]
            if path_part.startswith("./") or not path_part.startswith("/"):
                # Relative path - resolve against repo root
                repo_root = Path(__file__).parent.parent.parent
                resolved = (repo_root / path_part).absolute()
                return f"sqlite:///{resolved}"
        
        return self.database_url
    
    # Topic watch settings (ADR-0005: X access polling)
    # Set 2026-11-21: 3h poll interval matches ADR-0005
    watch_poll_interval_hours: int = 3
    watch_max_results: int = 10
    
    # Daily read cap (ADR-0005: target ~300 posts/day across topics)
    # Set 2026-11-21: Cost is $0.005/post, so 300 posts = $1.50/day
    daily_post_read_cap: int = 300
    
    # Review settings
    review_timeout_hours: int = 48
    
    # Confidence threshold (ADR-0006: computed confidence)
    # Set 2026-11-21: Threshold 0.6 with guessed weights, revisit after first 50 real cards
    confidence_threshold: float = 0.6
    
    # Follow-up settings (ADR-0002: follow-up timer)
    # Set 2026-11-21: 3 days after confirmation
    followup_delay_days: int = 3
    # Optional: override with seconds for E2E testing (takes precedence over days)
    followup_delay_seconds: Optional[int] = None
    
    # Activity timeouts (Temporal)
    # Set 2026-11-21: timeouts for activity execution
    activity_timeout_short_sec: int = 10  # DB operations
    activity_timeout_medium_sec: int = 30  # X API reads
    activity_timeout_long_sec: int = 60  # LLM calls
    
    # Retry policy
    activity_max_attempts: int = 3
    
    # Claim gate settings
    claim_gate_max_attempts: int = 2
    
    # Continue-As-New threshold
    can_threshold_posts: int = 100
    
    # Adapter selection (B5: injected adapters, NB1: explicit selection)
    # Set 2026-11-21: "fake" for tests, "real" for production
    # Worker refuses to start if "real" is selected but X_BEARER_TOKEN is not configured
    x_client_adapter: str = "fake"
    reply_agent_adapter: str = "fake"
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
