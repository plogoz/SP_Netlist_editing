import networkx as nx

G = nx.Graph()

G.add_edges_from([(1, 2), (1, 3)])
G.add_node(1)
G.add_edge(1, 2)
G.add_node("spam")  # adds node "spam"
G.add_nodes_from("spam")  # adds 4 nodes: 's', 'p', 'a', 'm'
G.add_edge(3, "m")

G.nodes["spam"]["color"] = "blue"
G.edges[(1, 2)]["weight"] = 10

G.edges(data=True)
G.nodes(
    data="color"
)  # Pourquoi y'a une sortie qui donne 'spam' = blue et s, p, a et m donnent None ?
print(G)

# G.number_of_nodes()
# G.number_of_edges()
