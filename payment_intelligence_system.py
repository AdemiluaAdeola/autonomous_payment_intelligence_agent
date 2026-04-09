"""
Autonomous Payment Intelligence Agent System
Multi-agent AI system for payment processing, fraud detection, gateway routing, and reconciliation
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum
from dataclasses import dataclass, field, asdict
from uuid import uuid4
import json

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import structlog
from contextlib import asynccontextmanager

import numpy as np
from sklearn.ensemble import IsolationForest
from difflib import SequenceMatcher
import aioredis
from datetime import datetime as dt

# ==================== DATA MODELS ====================

class GatewayType(str, Enum):
    STRIPE = "stripe"
    PAYPAL = "paypal"
    CRYPTO = "crypto"


class TransactionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    BLOCKED = "blocked"
    FAILED = "failed"
    RECONCILED = "reconciled"


@dataclass
class Transaction:
    """Transaction data structure"""
    id: str
    customer_id: str
    amount: float
    currency: str
    gateway: GatewayType
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self):
        return {
            **asdict(self),
            'gateway': self.gateway.value,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class AnomalyScore:
    """Fraud risk assessment"""
    transaction_id: str
    isolation_forest_score: float
    statistical_anomaly_score: float
    velocity_risk: float
    pattern_risk: float
    combined_risk_score: float
    risk_level: str  # "low", "medium", "high", "critical"
    flags: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class RoutingDecision:
    """Gateway routing recommendation"""
    transaction_id: str
    selected_gateway: GatewayType
    reason: str
    cost_optimization: float  # percentage savings
    reliability_score: float  # 0-1
    estimated_processing_time_ms: int


@dataclass
class ReconciliationMatch:
    """Payment-to-invoice matching result"""
    transaction_id: str
    invoice_id: Optional[str]
    confidence_score: float
    fuzzy_match_ratio: float
    amount_variance: float
    matched: bool


# ==================== PYDANTIC MODELS FOR API ====================

class TransactionRequest(BaseModel):
    customer_id: str
    amount: float = Field(gt=0)
    currency: str = Field(default="USD")
    gateway: GatewayType
    description: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PaymentResponse(BaseModel):
    transaction_id: str
    status: TransactionStatus
    message: str
    risk_score: float
    selected_gateway: GatewayType
    processing_time_ms: float


class InvoiceData(BaseModel):
    invoice_id: str
    customer_id: str
    amount: float
    description: str
    created_at: datetime


# ==================== MONITORING AGENT ====================

class MonitoringAgent:
    """Ingests and normalizes transactions from multiple payment gateways"""
    
    def __init__(self, logger: structlog.BoundLogger):
        self.logger = logger
        self.transaction_buffer: Dict[str, Transaction] = {}
        
    async def ingest_transaction(self, request: TransactionRequest) -> Transaction:
        """Normalize transaction from any gateway"""
        transaction = Transaction(
            id=str(uuid4()),
            customer_id=request.customer_id,
            amount=request.amount,
            currency=request.currency,
            gateway=request.gateway,
            timestamp=datetime.now(),
            metadata=request.metadata
        )
        
        self.transaction_buffer[transaction.id] = transaction
        self.logger.info(
            "transaction_ingested",
            transaction_id=transaction.id,
            amount=transaction.amount,
            gateway=transaction.gateway.value,
            customer_id=transaction.customer_id
        )
        return transaction


# ==================== ANOMALY DETECTION AGENT ====================

class AnomalyDetectionAgent:
    """ML-based fraud detection using Isolation Forest + statistical analysis"""
    
    def __init__(self, logger: structlog.BoundLogger):
        self.logger = logger
        self.isolation_forest = IsolationForest(
            contamination=0.05,
            random_state=42,
            n_estimators=100
        )
        self.transaction_history: List[Transaction] = []
        self.trained = False
        self._initialize_model()
        
    def _initialize_model(self):
        """Initialize with synthetic training data"""
        np.random.seed(42)
        normal_amounts = np.random.gamma(2, 2, 500)
        anomalous_amounts = np.concatenate([
            np.random.gamma(5, 20, 25),  # Unusually large
            np.random.gamma(0.1, 0.1, 25)  # Unusually small
        ])
        
        X_train = np.concatenate([
            normal_amounts[:475],
            anomalous_amounts[:50]
        ]).reshape(-1, 1)
        
        self.isolation_forest.fit(X_train)
        self.trained = True
        self.logger.info("anomaly_model_initialized")
        
    async def detect_anomalies(self, transaction: Transaction) -> AnomalyScore:
        """Comprehensive fraud risk assessment"""
        flags = []
        scores = {}
        
        # 1. Isolation Forest Score (ML-based)
        amount_reshaped = np.array([[transaction.amount]])
        if_prediction = self.isolation_forest.predict(amount_reshaped)[0]
        if_score = self.isolation_forest.score_samples(amount_reshaped)[0]
        isolation_forest_score = max(0, -if_score / 2)  # Normalize to 0-1
        
        if if_prediction == -1:
            flags.append("ml_anomaly_detected")
            scores["isolation_forest"] = min(1.0, isolation_forest_score)
        else:
            scores["isolation_forest"] = isolation_forest_score
        
        # 2. Statistical Analysis
        statistical_score = 0.0
        
        # Pattern: Micro-transactions (< $1)
        if transaction.amount < 1.0:
            flags.append("micro_transaction")
            statistical_score += 0.3
        
        # Pattern: Round numbers (likely fraud)
        if transaction.amount % 100 == 0 or transaction.amount % 50 == 0:
            if transaction.amount > 100:  # Only flag if significant
                flags.append("suspicious_round_amount")
                statistical_score += 0.2
        
        # Pattern: Very large amounts
        if transaction.amount > 50000:
            flags.append("unusually_large_amount")
            statistical_score += 0.15
        
        scores["statistical"] = statistical_score
        
        # 3. Velocity Check (5+ transactions in 5 minutes)
        velocity_score = await self._check_velocity(transaction)
        if velocity_score > 0.5:
            flags.append(f"high_velocity_detected")
        scores["velocity"] = velocity_score
        
        # 4. Pattern Detection
        pattern_score = self._analyze_patterns(transaction)
        if pattern_score > 0.3:
            flags.append("suspicious_pattern")
        scores["pattern"] = pattern_score
        
        # Combined risk score (weighted)
        combined_score = (
            isolation_forest_score * 0.4 +
            statistical_score * 0.3 +
            velocity_score * 0.2 +
            pattern_score * 0.1
        )
        
        # Determine risk level
        if combined_score > 0.95:
            risk_level = "critical"
        elif combined_score > 0.75:
            risk_level = "high"
        elif combined_score > 0.5:
            risk_level = "medium"
        else:
            risk_level = "low"
        
        anomaly = AnomalyScore(
            transaction_id=transaction.id,
            isolation_forest_score=isolation_forest_score,
            statistical_anomaly_score=statistical_score,
            velocity_risk=velocity_score,
            pattern_risk=pattern_score,
            combined_risk_score=combined_score,
            risk_level=risk_level,
            flags=flags
        )
        
        self.logger.info(
            "anomaly_analysis_complete",
            transaction_id=transaction.id,
            risk_score=combined_score,
            risk_level=risk_level,
            flags=flags
        )
        
        return anomaly
    
    async def _check_velocity(self, transaction: Transaction) -> float:
        """Detect rapid-fire transactions from same customer"""
        recent = [
            t for t in self.transaction_history
            if t.customer_id == transaction.customer_id
            and (datetime.now() - t.timestamp).total_seconds() < 300  # 5 minutes
        ]
        
        velocity_count = len(recent)
        if velocity_count > 5:
            return min(1.0, (velocity_count - 5) / 10)  # Scale: 5+ → 0.5-1.0
        return 0.0
    
    def _analyze_patterns(self, transaction: Transaction) -> float:
        """Analyze transaction patterns for red flags"""
        score = 0.0
        
        # Check for specific suspicious metadata patterns
        if "test" in str(transaction.metadata).lower():
            score += 0.3
        
        if transaction.currency not in ["USD", "EUR", "GBP", "BTC", "ETH"]:
            score += 0.1  # Unusual currency
        
        return min(1.0, score)
    
    def add_to_history(self, transaction: Transaction):
        """Add transaction to historical data"""
        self.transaction_history.append(transaction)
        # Keep last 10000 transactions for memory efficiency
        if len(self.transaction_history) > 10000:
            self.transaction_history = self.transaction_history[-10000:]


# ==================== ROUTING AGENT ====================

class RoutingAgent:
    """Intelligent gateway selection with cost optimization"""
    
    GATEWAY_CONFIG = {
        GatewayType.STRIPE: {
            "cost_per_transaction": 0.029,  # 2.9% + $0.30
            "reliability": 0.999,
            "max_amount": 999999,
            "processing_time_ms": 200,
            "supports_currencies": ["USD", "EUR", "GBP", "JPY"],
        },
        GatewayType.PAYPAL: {
            "cost_per_transaction": 0.034,  # 3.4% + $0.30
            "reliability": 0.998,
            "max_amount": 500000,
            "processing_time_ms": 300,
            "supports_currencies": ["USD", "EUR", "GBP"],
        },
        GatewayType.CRYPTO: {
            "cost_per_transaction": 0.001,  # 0.1%
            "reliability": 0.95,
            "max_amount": 1000000,
            "processing_time_ms": 2000,
            "supports_currencies": ["BTC", "ETH", "USDC"],
        }
    }
    
    def __init__(self, logger: structlog.BoundLogger):
        self.logger = logger
    
    async def route_transaction(
        self,
        transaction: Transaction,
        anomaly_score: AnomalyScore
    ) -> RoutingDecision:
        """Select optimal gateway based on risk, amount, and cost"""
        
        reasons = []
        
        # Rule 1: High-risk transactions → Stripe (most secure)
        if anomaly_score.combined_risk_score > 0.95:
            return RoutingDecision(
                transaction_id=transaction.id,
                selected_gateway=GatewayType.STRIPE,
                reason="High-risk transaction routed to most secure gateway",
                cost_optimization=0,
                reliability_score=self.GATEWAY_CONFIG[GatewayType.STRIPE]["reliability"],
                estimated_processing_time_ms=self.GATEWAY_CONFIG[GatewayType.STRIPE]["processing_time_ms"]
            )
        
        # Rule 2: Crypto currency → Crypto gateway
        if transaction.currency in ["BTC", "ETH", "USDC"]:
            return RoutingDecision(
                transaction_id=transaction.id,
                selected_gateway=GatewayType.CRYPTO,
                reason="Cryptocurrency detected, routing to crypto gateway",
                cost_optimization=96.7,  # vs Stripe
                reliability_score=self.GATEWAY_CONFIG[GatewayType.CRYPTO]["reliability"],
                estimated_processing_time_ms=self.GATEWAY_CONFIG[GatewayType.CRYPTO]["processing_time_ms"]
            )
        
        # Rule 3: Large amounts (> $10K) → Most reliable gateway
        if transaction.amount > 10000:
            return RoutingDecision(
                transaction_id=transaction.id,
                selected_gateway=GatewayType.STRIPE,
                reason="Large amount ($10K+) routed to most reliable gateway",
                cost_optimization=0,
                reliability_score=self.GATEWAY_CONFIG[GatewayType.STRIPE]["reliability"],
                estimated_processing_time_ms=self.GATEWAY_CONFIG[GatewayType.STRIPE]["processing_time_ms"]
            )
        
        # Rule 4: Cost optimization for medium risk
        if anomaly_score.combined_risk_score < 0.5 and transaction.amount < 10000:
            if transaction.currency in self.GATEWAY_CONFIG[GatewayType.PAYPAL]["supports_currencies"]:
                paypal_config = self.GATEWAY_CONFIG[GatewayType.PAYPAL]
                stripe_config = self.GATEWAY_CONFIG[GatewayType.STRIPE]
                savings = ((stripe_config["cost_per_transaction"] - paypal_config["cost_per_transaction"]) 
                          / stripe_config["cost_per_transaction"] * 100)
                
                return RoutingDecision(
                    transaction_id=transaction.id,
                    selected_gateway=GatewayType.PAYPAL,
                    reason="Cost optimization for low-risk transaction",
                    cost_optimization=savings,
                    reliability_score=paypal_config["reliability"],
                    estimated_processing_time_ms=paypal_config["processing_time_ms"]
                )
        
        # Default: Stripe (best overall)
        stripe_config = self.GATEWAY_CONFIG[GatewayType.STRIPE]
        return RoutingDecision(
            transaction_id=transaction.id,
            selected_gateway=GatewayType.STRIPE,
            reason="Default gateway for standard processing",
            cost_optimization=0,
            reliability_score=stripe_config["reliability"],
            estimated_processing_time_ms=stripe_config["processing_time_ms"]
        )


# ==================== RECONCILIATION AGENT ====================

class ReconciliationAgent:
    """NLP-based fuzzy matching for payment-to-invoice reconciliation"""
    
    def __init__(self, logger: structlog.BoundLogger):
        self.logger = logger
        self.invoices: List[InvoiceData] = []
    
    def add_invoice(self, invoice: InvoiceData):
        """Register invoice for reconciliation"""
        self.invoices.append(invoice)
    
    async def reconcile_transaction(
        self,
        transaction: Transaction
    ) -> ReconciliationMatch:
        """Match payment to invoice using fuzzy matching"""
        
        if not self.invoices:
            self.logger.warning("no_invoices_registered", transaction_id=transaction.id)
            return ReconciliationMatch(
                transaction_id=transaction.id,
                invoice_id=None,
                confidence_score=0.0,
                fuzzy_match_ratio=0.0,
                amount_variance=0.0,
                matched=False
            )
        
        best_match = None
        best_score = 0.0
        
        for invoice in self.invoices:
            # Customer match check
            if invoice.customer_id != transaction.customer_id:
                continue
            
            # Amount variance check (within 5%)
            amount_variance = abs(transaction.amount - invoice.amount) / invoice.amount
            if amount_variance > 0.05:
                continue
            
            # Fuzzy match on description (if provided)
            fuzzy_score = self._fuzzy_match(
                transaction.metadata.get("description", ""),
                invoice.description
            )
            
            # Combined confidence
            confidence = (1 - amount_variance) * 0.6 + fuzzy_score * 0.4
            
            if confidence > best_score:
                best_score = confidence
                best_match = invoice
        
        if best_match and best_score > 0.7:
            self.logger.info(
                "transaction_reconciled",
                transaction_id=transaction.id,
                invoice_id=best_match.invoice_id,
                confidence=best_score
            )
            return ReconciliationMatch(
                transaction_id=transaction.id,
                invoice_id=best_match.invoice_id,
                confidence_score=best_score,
                fuzzy_match_ratio=self._fuzzy_match(
                    transaction.metadata.get("description", ""),
                    best_match.description
                ),
                amount_variance=abs(transaction.amount - best_match.amount) / best_match.amount,
                matched=True
            )
        
        return ReconciliationMatch(
            transaction_id=transaction.id,
            invoice_id=None,
            confidence_score=best_score,
            fuzzy_match_ratio=0.0,
            amount_variance=0.0,
            matched=False
        )
    
    @staticmethod
    def _fuzzy_match(str1: str, str2: str) -> float:
        """Calculate fuzzy string matching ratio"""
        if not str1 or not str2:
            return 0.0
        return SequenceMatcher(None, str1.lower(), str2.lower()).ratio()


# ==================== FASTAPI APPLICATION ====================

# Global agent instances
monitoring_agent: Optional[MonitoringAgent] = None
anomaly_agent: Optional[AnomalyDetectionAgent] = None
routing_agent: Optional[RoutingAgent] = None
reconciliation_agent: Optional[ReconciliationAgent] = None

# Structured logging setup
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Transaction audit trail and statistics
transaction_log: List[Dict[str, Any]] = []
statistics = {
    "total_transactions": 0,
    "approved": 0,
    "blocked": 0,
    "reconciled": 0,
    "avg_processing_time_ms": 0,
    "fraud_detection_rate": 0.0,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager"""
    global monitoring_agent, anomaly_agent, routing_agent, reconciliation_agent
    
    # Startup
    monitoring_agent = MonitoringAgent(logger)
    anomaly_agent = AnomalyDetectionAgent(logger)
    routing_agent = RoutingAgent(logger)
    reconciliation_agent = ReconciliationAgent(logger)
    
    logger.info("payment_intelligence_system_started")
    
    yield
    
    logger.info("payment_intelligence_system_shutdown")


app = FastAPI(
    title="Autonomous Payment Intelligence Agent",
    description="Multi-agent AI system for payment processing and fraud detection",
    version="1.0.0",
    lifespan=lifespan
)


# ==================== API ENDPOINTS ====================

@app.post("/process-payment", response_model=PaymentResponse)
async def process_payment(request: TransactionRequest, background_tasks: BackgroundTasks):
    """
    Process a payment through the multi-agent system.
    Returns immediate response with decision and audit trail.
    """
    start_time = datetime.now()
    
    # Agent 1: Monitoring - Ingest transaction
    transaction = await monitoring_agent.ingest_transaction(request)
    
    # Agent 2: Anomaly Detection - Assess fraud risk
    anomaly_score = await anomaly_agent.detect_anomalies(transaction)
    anomaly_agent.add_to_history(transaction)
    
    # Decision: Block if critical risk
    if anomaly_score.combined_risk_score > 0.95:
        processing_time = (datetime.now() - start_time).total_seconds() * 1000
        
        audit_entry = {
            "transaction_id": transaction.id,
            "status": TransactionStatus.BLOCKED.value,
            "reason": "Autonomous risk block",
            "risk_score": anomaly_score.combined_risk_score,
            "flags": anomaly_score.flags,
            "processing_time_ms": processing_time,
            "timestamp": datetime.now().isoformat()
        }
        transaction_log.append(audit_entry)
        statistics["blocked"] += 1
        
        logger.info(
            "payment_blocked",
            transaction_id=transaction.id,
            risk_score=anomaly_score.combined_risk_score
        )
        
        return PaymentResponse(
            transaction_id=transaction.id,
            status=TransactionStatus.BLOCKED,
            message="Transaction blocked due to high fraud risk",
            risk_score=anomaly_score.combined_risk_score,
            selected_gateway=GatewayType.STRIPE,
            processing_time_ms=processing_time
        )
    
    # Agent 3: Routing - Select optimal gateway
    routing_decision = await routing_agent.route_transaction(transaction, anomaly_score)
    
    # Agent 4: Reconciliation - Match to invoice (async)
    reconciliation_result = await reconciliation_agent.reconcile_transaction(transaction)
    
    processing_time = (datetime.now() - start_time).total_seconds() * 1000
    
    # Audit trail
    audit_entry = {
        "transaction_id": transaction.id,
        "status": TransactionStatus.APPROVED.value,
        "customer_id": transaction.customer_id,
        "amount": transaction.amount,
        "currency": transaction.currency,
        "risk_score": anomaly_score.combined_risk_score,
        "risk_level": anomaly_score.risk_level,
        "selected_gateway": routing_decision.selected_gateway.value,
        "routing_reason": routing_decision.reason,
        "cost_optimization_percent": routing_decision.cost_optimization,
        "reconciled": reconciliation_result.matched,
        "invoice_id": reconciliation_result.invoice_id,
        "reconciliation_confidence": reconciliation_result.confidence_score,
        "processing_time_ms": processing_time,
        "timestamp": datetime.now().isoformat()
    }
    transaction_log.append(audit_entry)
    
    # Update statistics
    statistics["total_transactions"] += 1
    statistics["approved"] += 1
    if reconciliation_result.matched:
        statistics["reconciled"] += 1
    statistics["avg_processing_time_ms"] = (
        statistics["avg_processing_time_ms"] * 0.9 + processing_time * 0.1
    )
    
    logger.info(
        "payment_approved",
        transaction_id=transaction.id,
        gateway=routing_decision.selected_gateway.value,
        processing_time_ms=processing_time
    )
    
    return PaymentResponse(
        transaction_id=transaction.id,
        status=TransactionStatus.APPROVED,
        message=f"Payment approved and routed to {routing_decision.selected_gateway.value}",
        risk_score=anomaly_score.combined_risk_score,
        selected_gateway=routing_decision.selected_gateway,
        processing_time_ms=processing_time
    )


@app.post("/register-invoice")
async def register_invoice(invoice: InvoiceData):
    """Register an invoice for payment reconciliation"""
    reconciliation_agent.add_invoice(invoice)
    logger.info(
        "invoice_registered",
        invoice_id=invoice.invoice_id,
        customer_id=invoice.customer_id,
        amount=invoice.amount
    )
    return {"status": "registered", "invoice_id": invoice.invoice_id}


@app.get("/health")
async def health_check():
    """System health and readiness check"""
    return {
        "status": "healthy",
        "agents": {
            "monitoring": monitoring_agent is not None,
            "anomaly_detection": anomaly_agent is not None and anomaly_agent.trained,
            "routing": routing_agent is not None,
            "reconciliation": reconciliation_agent is not None,
        },
        "timestamp": datetime.now().isoformat()
    }


@app.get("/statistics")
async def get_statistics():
    """Get system statistics and performance metrics"""
    fraud_rate = (
        statistics["blocked"] / statistics["total_transactions"]
        if statistics["total_transactions"] > 0 else 0.0
    )
    
    return {
        "total_transactions_processed": statistics["total_transactions"],
        "approved_count": statistics["approved"],
        "blocked_count": statistics["blocked"],
        "reconciled_count": statistics["reconciled"],
        "fraud_detection_rate": fraud_rate,
        "avg_processing_time_ms": round(statistics["avg_processing_time_ms"], 2),
        "approval_rate": (
            statistics["approved"] / statistics["total_transactions"]
            if statistics["total_transactions"] > 0 else 0.0
        ),
        "transaction_log_size": len(transaction_log),
        "timestamp": datetime.now().isoformat()
    }


@app.get("/audit-trail")
async def get_audit_trail(
    transaction_id: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
    risk_level: Optional[str] = Query(None)
):
    """
    Retrieve audit trail with filtering options.
    Complete transparency of all agent decisions.
    """
    results = transaction_log
    
    if transaction_id:
        results = [t for t in results if t.get("transaction_id") == transaction_id]
    
    if risk_level:
        results = [t for t in results if t.get("risk_level") == risk_level]
    
    return {
        "total_records": len(results),
        "records": results[-limit:],
        "timestamp": datetime.now().isoformat()
    }


@app.get("/agent-status")
async def get_agent_status():
    """Detailed agent status and configuration"""
    return {
        "monitoring_agent": {
            "status": "active",
            "buffered_transactions": len(monitoring_agent.transaction_buffer)
        },
        "anomaly_detection_agent": {
            "status": "active",
            "model_type": "Isolation Forest",
            "model_trained": anomaly_agent.trained,
            "history_size": len(anomaly_agent.transaction_history),
            "contamination_rate": 0.05
        },
        "routing_agent": {
            "status": "active",
            "gateways_configured": list(RoutingAgent.GATEWAY_CONFIG.keys()),
            "gateway_details": {
                gateway.value: {
                    "cost_percent": config["cost_per_transaction"],
                    "reliability": config["reliability"],
                    "max_amount": config["max_amount"]
                }
                for gateway, config in RoutingAgent.GATEWAY_CONFIG.items()
            }
        },
        "reconciliation_agent": {
            "status": "active",
            "invoices_registered": len(reconciliation_agent.invoices),
            "matching_algorithm": "Fuzzy String Matching"
        },
        "timestamp": datetime.now().isoformat()
    }


@app.get("/")
async def root():
    """API documentation endpoint"""
    return {
        "service": "Autonomous Payment Intelligence Agent",
        "version": "1.0.0",
        "endpoints": {
            "POST /process-payment": "Process a payment through all agents",
            "POST /register-invoice": "Register invoice for reconciliation",
            "GET /health": "Health check",
            "GET /statistics": "System statistics",
            "GET /audit-trail": "Complete transaction audit trail",
            "GET /agent-status": "Detailed agent status",
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
