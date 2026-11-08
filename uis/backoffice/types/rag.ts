export interface KnowledgeSource {
  source_document: string;
  section: string;
  retrieval_kind: "primary" | "complementary";
}

export interface KnowledgeQueryResponse {
  answer: string;
  insufficient_context: boolean;
  sources: KnowledgeSource[];
}
