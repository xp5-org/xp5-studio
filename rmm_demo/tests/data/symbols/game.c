struct player { int x, y; int health; int ammo; };
struct player hero = {1,2,100,50};
int score;
void hurt(int d) { hero.health -= d; score++; }
int main(void) { int i; for (i = 0; i < 3; i++) hurt(1); return hero.health; }
