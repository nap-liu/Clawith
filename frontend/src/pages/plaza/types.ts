export interface Post {
    id: string;
    author_id: string;
    author_type: 'agent' | 'human';
    author_name: string;
    content: string;
    likes_count: number;
    comments_count: number;
    created_at: string;
    comments?: Comment[];
}

interface Comment {
    id: string;
    post_id: string;
    author_id: string;
    author_type: 'agent' | 'human';
    author_name: string;
    content: string;
    created_at: string;
}

export interface PlazaStats {
    total_posts: number;
    total_comments: number;
    today_posts: number;
    top_contributors: { name: string; type: string; posts: number }[];
}

export interface Agent {
    id: string;
    name: string;
    status: string;
    avatar?: string;
}
