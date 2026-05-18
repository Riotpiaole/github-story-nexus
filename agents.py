from typing import TypedDict, Literal
from langgraph.graph import StateGraph, START, END
from state import *

# 4. Build the State Machine Graph
workflow = StateGraph(AgentState)

# Add Nodes
workflow.add_node("read_issue", read_github_issue)
workflow.add_node("code", code_solution)
workflow.add_node("test", test_code)
workflow.add_node("fail_state", handle_failure)
workflow.add_node("create_pr", create_pr)

# Add Linear Edges
workflow.add_edge(START, "read_issue")
workflow.add_edge("read_issue", "code")
workflow.add_edge("code", "test")

# Add Conditional Edges from the 'test' node
workflow.add_conditional_edges(
    "test",
    route_after_test,
    {
        "create_pr": "create_pr",
        "code_solution": "code",
        "handle_failure": "fail_state"
    }
)

# Add End Edges
workflow.add_edge("create_pr", END)
workflow.add_edge("fail_state", END)

# Compile the Graph
app = workflow.compile()

# 5. Run the Graph
config = {"configurable": {"thread_id": "1"}}
initial_input = {}
events = app.stream(initial_input, config)

for event in events:
    print(event)
